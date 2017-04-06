# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import date, datetime, timedelta
from os import remove
from os.path import exists, getsize, join
from time import sleep

import geojson
import homura
import html2text
import pycurl
import requests
from tqdm import tqdm

from six import string_types
from six.moves.urllib.parse import urljoin

from . import __version__ as sentinelsat_version

try:
    import certifi
except ImportError:
    certifi = None


class SentinelAPI(object):
    """Class to connect to Sentinel Data Hub, search and download imagery.

    Parameters
    ----------
    user : string
        username for DataHub
    password : string
        password for DataHub
    api_url : string, optional
        URL of the DataHub
        defaults to 'https://scihub.copernicus.eu/apihub'

    Attributes
    ----------
    session : requests.Session object
        Session to connect to DataHub
    api_url : str
        URL to the DataHub
    page_size : int
        number of results per query page
        current value: 100 (maximum allowed on ApiHub)
    """

    logger = logging.getLogger('sentinelsat.SentinelAPI')

    def __init__(self, user, password, api_url='https://scihub.copernicus.eu/apihub/'):
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.api_url = api_url if api_url.endswith('/') else api_url + '/'
        self.page_size = 100
        self.user_agent = 'sentinelsat/' + sentinelsat_version
        self.session.headers['User-Agent'] = self.user_agent
        # For unit tests
        self._last_query = None
        self._last_status_code = None

    def query(self, area=None, initial_date='NOW-1DAY', end_date='NOW', **keywords):
        """Query the SciHub API with the coordinates of an area, a date interval
        and any other search keywords accepted by the SciHub API.
        """
        query = self.format_query(area, initial_date, end_date, **keywords)
        return self.query_plain(query)

    @staticmethod
    def format_query(area=None, initial_date='NOW-1DAY', end_date='NOW', **keywords):
        """Create the SciHub API query string
        """
        query_parts = []
        if initial_date is not None and end_date is not None:
            query_parts += ['(beginPosition:[%s TO %s])' % (
                _format_date(initial_date),
                _format_date(end_date)
            )]

        if area is not None:
            query_parts += ['(footprint:"Intersects(%s)")' % area]

        for kw in sorted(keywords):
            query_parts += ['(%s:%s)' % (kw, keywords[kw])]

        query = ' AND '.join(query_parts)
        return query

    def query_plain(self, query):
        """Do a full-text query on the SciHub API using the OpenSearch format specified in
           https://scihub.copernicus.eu/twiki/do/view/SciHubUserGuide/3FullTextSearch
        """
        response = self._load_query(query)
        return _response_to_dict(response)

    def _load_query(self, query, start_row=0):
        # store last query (for testing)
        self._last_query = query

        # load query results
        url = self._format_url(start_row=start_row)
        response = self.session.post(url, dict(q=query), auth=self.session.auth)
        _check_scihub_response(response)

        # store last status code (for testing)
        self._last_status_code = response.status_code

        # parse response content
        try:
            json_feed = response.json()['feed']
            total_results = int(json_feed['opensearch:totalResults'])
        except (ValueError, KeyError):
            raise SentinelAPIError(http_status=response.status_code,
                                   msg='API response not valid. JSON decoding failed.',
                                   response_body=response.content)

        entries = json_feed.get('entry', [])
        # this verification is necessary because if the query returns only
        # one product, self.products will be a dict not a list
        if isinstance(entries, dict):
            entries = [entries]

        output = entries
        # repeat query until all results have been loaded
        if total_results > start_row + self.page_size - 1:
            output += self._load_query(query, start_row=(start_row + self.page_size))
        return output

    @staticmethod
    def to_geojson(products):
        """Return the products from a query response as a GeoJSON with the values in their appropriate Python types.
        """
        feature_list = []
        for i, (product_id, props) in enumerate(products.items()):
            props = props.copy()
            props['id'] = product_id
            poly = _geojson_poly_from_wkt(props['footprint'])
            del props['footprint']
            del props['gmlfootprint']
            # Fix "'datetime' is not JSON serializable"
            for k, v in props.items():
                if isinstance(v, (date, datetime)):
                    props[k] = v.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            feature_list.append(
                geojson.Feature(geometry=poly, id=i, properties=props)
            )
        return geojson.FeatureCollection(feature_list)

    @staticmethod
    def to_dataframe(products):
        """Return the products from a query response as a Pandas DataFrame
        with the values in their appropriate Python types.
        """
        import pandas as pd

        return pd.DataFrame.from_dict(products, orient='index')

    @staticmethod
    def to_geodataframe(products):
        """Return the products from a query response as a GeoPandas GeoDataFrame
        with the values in their appropriate Python types.
        """
        import geopandas as gpd
        import shapely.wkt

        df = SentinelAPI.to_dataframe(products)
        crs = {'init': 'epsg:4326'}  # WGS84
        geometry = [shapely.wkt.loads(fp) for fp in df['footprint']]
        # remove useless columns
        df.drop(['footprint', 'gmlfootprint'], axis=1, inplace=True)
        return gpd.GeoDataFrame(df, crs=crs, geometry=geometry)

    def get_product_odata(self, id):
        """Access SciHub OData API to get info about a Product. Returns a dict
        containing the id, title, size, md5sum, date, footprint and download url
        of the Product. The date field receives the Start ContentDate of the API.
        """

        response = self.session.get(
            urljoin(self.api_url, "odata/v1/Products('%s')/?$format=json" % id)
        )
        _check_scihub_response(response)

        d = response.json()['d']

        # parse the GML footprint to same format as returned
        # by .get_coordinates()
        geometry_xml = ET.fromstring(d["ContentGeometry"])
        poly_coords_str = geometry_xml \
            .find('{http://www.opengis.net/gml}outerBoundaryIs') \
            .find('{http://www.opengis.net/gml}LinearRing') \
            .findtext('{http://www.opengis.net/gml}coordinates')
        poly_coords = (coord.split(",")[::-1] for coord in poly_coords_str.split(" "))
        coord_string = ",".join(" ".join(coord) for coord in poly_coords)

        values = {
            'id': d['Id'],
            'title': d['Name'],
            'size': int(d['ContentLength']),
            'md5': d['Checksum']['Value'],
            'date': _convert_timestamp(d['ContentDate']['Start']),
            'footprint': coord_string,
            'url': urljoin(self.api_url, "odata/v1/Products('%s')/$value" % id)
        }
        return values

    def download(self, id, directory_path='.', checksum=False, check_existing=False, **kwargs):
        """Download a product using homura.

        Uses the filename on the server for the downloaded file, e.g.
        "S1A_EW_GRDH_1SDH_20141003T003840_20141003T003920_002658_002F54_4DD1.zip".

        Incomplete downloads are continued and complete files are skipped.

        Further keyword arguments are passed to the homura.download() function.

        Parameters
        ----------
        id : string
            UUID of the product, e.g. 'a8dd0cfd-613e-45ce-868c-d79177b916ed'
        directory_path : string, optional
            Where the file will be downloaded
        checksum : bool, optional
            If True, verify the downloaded file's integrity by checking its MD5 checksum.
            Throws InvalidChecksumError if the checksum does not match.
            Defaults to False.
        check_existing : bool, optional
            If True and a fully downloaded file with the same name exists on the disk,
            verify its integrity using its MD5 checksum. Re-download in case of non-matching checksums.
            Defaults to False.

        Returns
        -------
        product_info : dict
            Dictionary containing the product's info from get_product_info() as well as the path on disk.

        Raises
        ------
        InvalidChecksumError
            If the MD5 checksum does not match the checksum on the server.
        """
        # Check if API is reachable.
        product_info = None
        while product_info is None:
            try:
                product_info = self.get_product_odata(id)
            except SentinelAPIError as e:
                self.logger.info("Invalid API response:\n{}\nTrying again in 1 minute.".format(str(e)))
                sleep(60)

        path = join(directory_path, product_info['title'] + '.zip')
        product_info['path'] = path
        kwargs = _fillin_cainfo(kwargs)

        self.logger.info('Downloading %s to %s' % (id, path))

        # Check if the file exists and passes md5 test
        # Homura will by default continue the download if the file exists but is incomplete
        if exists(path) and getsize(path) == product_info['size']:
            if not check_existing or _md5_compare(path, product_info['md5']):
                self.logger.info('%s was already downloaded.' % path)
                return product_info
            else:
                self.logger.info(
                    '%s was already downloaded but is corrupt: checksums do not match. Re-downloading.' % path)
                remove(path)

        if (exists(path) and getsize(path) >= 2 ** 31 and
                    pycurl.version.split()[0].lower() <= 'pycurl/7.43.0'):
            # Workaround for PycURL's bug when continuing > 2 GB files
            # https://github.com/pycurl/pycurl/issues/405
            remove(path)

        homura.download(product_info['url'], path=path, auth=self.session.auth,
                        user_agent=self.user_agent, **kwargs)

        # Check integrity with MD5 checksum
        if checksum is True:
            if not _md5_compare(path, product_info['md5']):
                remove(path)
                raise InvalidChecksumError('File corrupt: checksums do not match')
        return product_info

    def download_all(self, products, directory_path='.', max_attempts=10, checksum=False, check_existing=False,
                     **kwargs):
        """Download all products returned in query().

        File names on the server are used for the downloaded files, e.g.
        "S1A_EW_GRDH_1SDH_20141003T003840_20141003T003920_002658_002F54_4DD1.zip".

        In case of interruptions or other exceptions, downloading will restart from where it left off.
        Downloading is attempted at most max_attempts times to avoid getting stuck with unrecoverable errors.

        Parameters
        ----------
        products : list
            List of products returned with self.query()
        directory_path : string
            Directory where the downloaded files will be downloaded
        max_attempts : int, optional
            Number of allowed retries before giving up downloading a product. Defaults to 10.

        Other Parameters
        ----------------
        See download().

        Returns
        -------
        dict[string, dict]
            A dictionary containing the return value from download() for each successfully downloaded product.
        set[string]
            The list of products that failed to download.
        """
        self.logger.info("Will download %d products" % len(products))
        return_values = OrderedDict()
        for i, product_id in enumerate(products):
            for attempt_num in range(max_attempts):
                try:
                    product_info = self.download(product_id, directory_path, checksum, check_existing, **kwargs)
                    return_values[product_id] = product_info
                    break
                except (KeyboardInterrupt, SystemExit):
                    raise
                except InvalidChecksumError:
                    self.logger.warning("Invalid checksum. The downloaded file for '{}' is corrupted.".format(product_id))
                except:
                    self.logger.exception("There was an error downloading %s" % product_id)
            self.logger.info("{}/{} products downloaded".format(i + 1, len(products)))
        failed = set(products) - set(return_values)
        return return_values, failed

    @staticmethod
    def get_products_size(products):
        """Return the total file size in GB of all products in the query"""
        size_total = 0
        for title, props in products.items():
            size_product = props["size"]
            size_value = float(size_product.split(" ")[0])
            size_unit = str(size_product.split(" ")[1])
            if size_unit == "MB":
                size_value /= 1024.
            if size_unit == "KB":
                size_value /= 1024. * 1024.
            size_total += size_value
        return round(size_total, 2)

    def _format_url(self, start_row=0):
        blank = 'search?format=json&rows={rows}&start={start}'.format(
            rows=self.page_size, start=start_row
        )
        return urljoin(self.api_url, blank)


class SentinelAPIError(Exception):
    """Invalid responses from SciHub.
    """

    def __init__(self, http_status=None, code=None, msg=None, response_body=None):
        self.http_status = http_status
        self.code = code
        self.msg = msg
        self.response_body = response_body

    def __str__(self):
        return '(HTTP status: {0}, code: {1}) {2}'.format(
            self.http_status, self.code,
            ('\n' if '\n' in self.msg else '') + self.msg)


class InvalidChecksumError(Exception):
    """MD5 checksum of local file does not match the one from the server.
    """
    pass


def get_coordinates(geojson_file, feature_number=0):
    """Return the coordinates of a polygon of a GeoJSON file.

    Parameters
    ----------
    geojson_file : str
        location of GeoJSON file_path
    feature_number : int
        Feature to extract polygon from (in case of MultiPolygon
        FeatureCollection), defaults to first Feature

    Returns
    -------
    polygon coordinates
        string of comma separated coordinate tuples (lon, lat) to be used by SentinelAPI
    """
    geojson_obj = geojson.loads(open(geojson_file).read())
    if 'coordinates' in geojson_obj:
        geometry = geojson_obj
    else:
        geometry = geojson_obj['features'][feature_number]['geometry']
    coordinates = geometry['coordinates'][0]
    # precision of 7 decimals equals 1mm at the equator
    coordinates = ['%.7f %.7f' % (coord[0], coord[1]) for coord in coordinates]
    return 'POLYGON((%s))' % (','.join(coordinates))


def _fillin_cainfo(kwargs_dict):
    """Fill in the path of the PEM file containing the CA certificate.

    The priority is: 1. user provided path, 2. path to the cacert.pem
    bundle provided by certifi (if installed), 3. let pycurl use the
    system path where libcurl's cacert bundle is assumed to be stored,
    as established at libcurl build time.
    """
    try:
        cainfo = kwargs_dict['pass_through_opts'][pycurl.CAINFO]
    except KeyError:
        try:
            cainfo = certifi.where()
        except AttributeError:
            cainfo = None

    if cainfo is not None:
        pass_through_opts = kwargs_dict.get('pass_through_opts', {})
        pass_through_opts[pycurl.CAINFO] = cainfo
        kwargs_dict['pass_through_opts'] = pass_through_opts

    return kwargs_dict


def _format_date(in_date):
    """Format a date, datetime or a YYYYMMDD string input as YYYY-MM-DDThh:mm:ssZ
    or validate a string input as suitable for the full text search interface and return it.
    """
    if isinstance(in_date, (datetime, date)):
        return in_date.strftime('%Y-%m-%dT%H:%M:%SZ')

    in_date = in_date.strip()
    if re.fullmatch(r"NOW(?:-\d+(?:MONTH|DAY|HOUR|MINUTE)S?)?", in_date):
        return in_date
    if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", in_date):
        return in_date

    try:
        return datetime.strptime(in_date, '%Y%m%d').strftime('%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise ValueError('Unsupported date value {}'.format(in_date))


def _convert_timestamp(in_date):
    """Convert the timestamp received from OData JSON API, to
    YYYY-MM-DDThh:mm:ssZ string format.
    """
    in_date = int(in_date.replace('/Date(', '').replace(')/', '')) / 1000.
    return _format_date(datetime.utcfromtimestamp(in_date))


def _check_scihub_response(response):
    """Check that the response from server has status code 2xx and that the response is valid JSON."""
    try:
        response.raise_for_status()
        response.json()
    except (requests.HTTPError, ValueError) as e:
        msg = "API response not valid. JSON decoding failed."
        code = None
        try:
            msg = response.json()['error']['message']['value']
            code = response.json()['error']['code']
        except:
            if not response.text.rstrip().startswith('{'):
                try:
                    h = html2text.HTML2Text()
                    h.ignore_images = True
                    h.ignore_anchors = True
                    msg = h.handle(response.text).strip()
                except:
                    pass
        api_error = SentinelAPIError(response.status_code, code, msg, response.content)
        # Suppress "During handling of the above exception..." message
        # See PEP 409
        api_error.__cause__ = None
        raise api_error


def _geojson_poly_from_wkt(wkt):
    """Return a geojson Polygon object from a WKT string"""
    coordlist = re.search(r'\(\s*([^()]+)\s*\)', wkt).group(1)
    coord_list_split = (coord.split(' ') for coord in coordlist.split(','))
    poly = geojson.Polygon([[(float(coord[0]), float(coord[1])) for coord in coord_list_split]])
    return poly


def _response_to_dict(products):
    """Convert a query response to a dictionary.
     
    The resulting dictionary structure is {<product id>: {<property>: <value>}}.
    The property values are converted to their respective Python types unless `parse_values` is set to `False`.
    """

    def convert_date(content):
        if '.' in content:
            return datetime.strptime(content, '%Y-%m-%dT%H:%M:%S.%fZ')
        else:
            return datetime.strptime(content, '%Y-%m-%dT%H:%M:%SZ')

    converters = {'date': convert_date, 'int': int, 'long': int, 'float': float, 'double': float}
    # Keep the string type by default
    default_converter = lambda x: x

    output = OrderedDict()
    for prod in products:
        product_dict = {}
        prod_id = prod['id']
        output[prod_id] = product_dict
        for key in prod:
            if key == 'id':
                continue
            if isinstance(prod[key], string_types):
                product_dict[key] = prod[key]
            else:
                properties = prod[key]
                if isinstance(properties, dict):
                    properties = [properties]
                if key == 'link':
                    for p in properties:
                        name = 'link'
                        if 'rel' in p:
                            name = 'link_' + p['rel']
                        product_dict[name] = p['href']
                else:
                    f = converters.get(key, default_converter)
                    for p in properties:
                        try:
                            product_dict[p['name']] = f(p['content'])
                        except KeyError:  # Sentinel-3 has one element 'arr' which violates the name:content convention
                            product_dict[p['name']] = f(p['str'])
    return output


def _md5_compare(file_path, checksum, block_size=2 ** 13):
    """Compare a given md5 checksum with one calculated from a file"""
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        progress = tqdm(desc="MD5 checksumming", total=getsize(file_path), unit="B", unit_scale=True)
        while True:
            block_data = f.read(block_size)
            if not block_data:
                break
            md5.update(block_data)
            progress.update(len(block_data))
        progress.close()
    return md5.hexdigest().lower() == checksum.lower()
