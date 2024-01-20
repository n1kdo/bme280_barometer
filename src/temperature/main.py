#
# main.py -- this is the web server for the Raspberry Pi Pico W Temperature Reader.
#

__author__ = 'J. B. Otterson'
__copyright__ = 'Copyright 2022, J. B. Otterson N1KDO.'

#
# Copyright 2022, J. B. Otterson N1KDO.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
# OF THE POSSIBILITY OF SUCH DAMAGE.

import json
import os
import re
import time

import ntp
from array import array

from http_server import HttpServer
from morse_code import MorseCode
from utils import milliseconds, upython, safe_int


if upython:
    import machine
    import network
    import uasyncio as asyncio
    import bme280_float as bme280
    import micro_logging as logging
else:
    import asyncio
    import logging

    class Machine(object):
        """
        fake micropython stuff
        """

        @staticmethod
        def soft_reset():
            print('Machine.soft_reset()')

        class Pin(object):
            OUT = 1
            IN = 0
            PULL_UP = 0

            def __init__(self, name, options=0, value=0):
                self.value = value
                self.name = name
                self.options = options

            def on(self):
                self.value = 1
                pass

            def off(self):
                self.value = 0

            def value(self):
                return self.value

    machine = Machine()

if upython:
    onboard = machine.Pin('LED', machine.Pin.OUT, value=1)  # turn on right away
    # blinky = machine.Pin(2, machine.Pin.OUT, value=0)  # status LED
    button = machine.Pin(3, machine.Pin.IN, machine.Pin.PULL_UP)

BUFFER_SIZE = 4096
CONFIG_FILE = 'data/config.json'
CONTENT_DIR = 'content/'
CT_TEXT_TEXT = 'text/text'
CT_TEXT_HTML = 'text/html'
CT_APP_JSON = 'application/json'
CT_APP_WWW_FORM = 'application/x-www-form-urlencoded'
CT_MULTIPART_FORM = 'multipart/form-data'
DANGER_ZONE_FILE_NAMES = [
    'config.html',
    'files.html',
    'temperature.html',
]
DEFAULT_SECRET = 'temperature'
DEFAULT_SSID = 'sht30'
DEFAULT_TCP_PORT = 73
DEFAULT_WEB_PORT = 80
FILE_EXTENSION_TO_CONTENT_TYPE_MAP = {
    'gif': 'image/gif',
    'html': CT_TEXT_HTML,
    'ico': 'image/vnd.microsoft.icon',
    'json': CT_APP_JSON,
    'jpeg': 'image/jpeg',
    'jpg': 'image/jpeg',
    'png': 'image/png',
    'txt': CT_TEXT_TEXT,
    '*': 'application/octet-stream',
}
HYPHENS = '--'
HTTP_STATUS_TEXT = {
    200: 'OK',
    201: 'Created',
    202: 'Accepted',
    204: 'No Content',
    301: 'Moved Permanently',
    302: 'Moved Temporarily',
    304: 'Not Modified',
    400: 'Bad Request',
    401: 'Unauthorized',
    403: 'Forbidden',
    404: 'Not Found',
    409: 'Conflict',
    500: 'Internal Server Error',
    501: 'Not Implemented',
    502: 'Bad Gateway',
    503: 'Service Unavailable',
}
MP_START_BOUND = 1
MP_HEADERS = 2
MP_DATA = 3
MP_END_BOUND = 4

# globals...
last_temperature = 0
last_humidity = 0
last_pressure = 0
restart = False
port = None
http_server = HttpServer(content_dir='content/')
morse_code_sender = MorseCode(onboard)

MAX_SAMPLES = 240  # one sample every 10 minutes


class Samples:
    def __init__(self, max_samples):
        self.max_samples = max_samples
        self.next_sample = 0
        self.samples = bytearray(b'\xff' * max_samples)

    def add_sample(self, sample):
        self.samples[self.next_sample] = sample
        self.next_sample += 1
        if self.next_sample == self.max_samples:
            self.next_sample = 0

    def get_samples(self):
        read_sample = self.next_sample
        first = True
        while read_sample != self.next_sample or first:
            first = False
            yield self.samples[read_sample]
            read_sample += 1
            if read_sample == self.max_samples:
                read_sample = 0

    def get_samples_printable(self):
        read_sample = self.next_sample
        first = True
        while read_sample != self.next_sample or first:
            first = False
            yield f'{self.samples[read_sample]:02x}'
            read_sample += 1
            if read_sample == self.max_samples:
                read_sample = 0

    def get_samples_numbers(self):
        read_sample = self.next_sample
        first = True
        while read_sample != self.next_sample or first:
            first = False
            yield int(self.samples[read_sample])
            read_sample += 1
            if read_sample == self.max_samples:
                read_sample = 0

    def __str__(self):
        return ' '.join(self.get_samples_printable())


t_samples = Samples(MAX_SAMPLES)
h_samples = Samples(MAX_SAMPLES)
p_samples = Samples(MAX_SAMPLES)


def get_timestamp(tt=None):
    if tt is None:
        tt = time.gmtime()
    return f'{tt[0]:04d}-{tt[1]:02d}-{tt[2]:02d} {tt[3]:02d}:{tt[4]:02d}:{tt[5]:02d}Z'


def get_iso_8601_timestamp(tt=None):
    if tt is None:
        tt = time.gmtime()
    return f'{tt[0]:04d}-{tt[1]:02d}-{tt[2]:02d}T{tt[3]:02d}:{tt[4]:02d}:{tt[5]:02d}+00:00'


def read_config():
    config = {}
    try:
        with open(CONFIG_FILE, 'r') as config_file:
            config = json.load(config_file)
    except Exception as ex:
        print('failed to load configuration!', type(ex), ex)
    return config


def save_config(config):
    with open(CONFIG_FILE, 'w') as config_file:
        json.dump(config, config_file)


def valid_filename(filename):
    if filename is None:
        return False
    match = re.match('^[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?.[a-zA-Z0-9_-]+$', filename)
    if match is None:
        return False
    if match.group(0) != filename:
        return False
    extension = filename.split('.')[-1].lower()
    if http_server.FILE_EXTENSION_TO_CONTENT_TYPE_MAP.get(extension) is None:
        return False
    return True


def connect_to_network(config):
    config['hostname'] = 'sht30'
    network.country('US')

    ssid = config.get('SSID') or ''
    if len(ssid) == 0 or len(ssid) > 64:
        ssid = DEFAULT_SSID
    secret = config.get('secret') or ''
    if len(secret) > 64:
        secret = ''
    access_point_mode = config.get('ap_mode') or False

    if access_point_mode:
        print('Starting setup WLAN...')
        wlan = network.WLAN(network.AP_IF)
        wlan.active(False)
        wlan.config(pm=0xa11140)  # disable power save, this is a server.

        hostname = config.get('hostname')
        if hostname is not None:
            try:
                wlan.config(hostname=hostname)
            except ValueError as exc:
                print(f'hostname is still not supported on Pico W')

        # wlan.ifconfig(('10.0.0.1', '255.255.255.0', '0.0.0.0', '0.0.0.0'))

        """
        #define CYW43_AUTH_OPEN (0)                     ///< No authorisation required (open)
        #define CYW43_AUTH_WPA_TKIP_PSK   (0x00200002)  ///< WPA authorisation
        #define CYW43_AUTH_WPA2_AES_PSK   (0x00400004)  ///< WPA2 authorisation (preferred)
        #define CYW43_AUTH_WPA2_MIXED_PSK (0x00400006)  ///< WPA2/WPA mixed authorisation
        """
        ssid = DEFAULT_SSID
        secret = DEFAULT_SECRET
        if len(secret) == 0:
            security = 0
        else:
            security = 0x00400004  # CYW43_AUTH_WPA2_AES_PSK
        wlan.config(ssid=ssid, key=secret, security=security)
        wlan.active(True)
        print(wlan.active())
        print('ssid={}'.format(wlan.config('ssid')))
    else:
        print('Connecting to WLAN...')
        wlan = network.WLAN(network.STA_IF)
        wlan.config(pm=0xa11140)  # disable power save, this is a server.

        hostname = config.get('hostname')
        if hostname is not None:
            try:
                network.hostname(hostname)
            except ValueError as exc:
                print(f'hostname is still not supported on Pico W')

        is_dhcp = config.get('dhcp') or True
        if not is_dhcp:
            ip_address = config.get('ip_address')
            netmask = config.get('netmask')
            gateway = config.get('gateway')
            dns_server = config.get('dns_server')
            if ip_address is not None and netmask is not None and gateway is not None and dns_server is not None:
                print('setting up static IP')
                wlan.ifconfig((ip_address, netmask, gateway, dns_server))
            else:
                print('cannot use static IP, data is missing, configuring network with DHCP')
                wlan.ifconfig('dhcp')
        else:
            print('configuring network with DHCP')
            # wlan.ifconfig('dhcp')  #  this does not work.  network does not come up.  no errors, either.

        wlan.active(True)
        wlan.connect(ssid, secret)
        max_wait = 10
        while max_wait > 0:
            status = wlan.status()
            if status < 0 or status >= 3:
                break
            max_wait -= 1
            print('Waiting for connection to come up, status={}'.format(status))
            time.sleep(1)
        if wlan.status() != network.STAT_GOT_IP:
            morse_code_sender.send_message('ERR ')
            # return None
            raise RuntimeError('Network connection failed')

    status = wlan.ifconfig()
    ip_address = status[0]
    morse_message = 'A  {}  '.format(ip_address) if access_point_mode else '{} '.format(ip_address)
    morse_message = morse_message.replace('.', ' ')
    morse_code_sender.set_message(morse_message)
    print(morse_message)
    return ip_address


# noinspection PyUnusedLocal
async def slash_callback(http, verb, args, reader, writer, request_headers=None):  # callback for '/'
    http_status = 301
    bytes_sent = http.send_simple_response(writer, http_status, None, None, ['Location: /temperature.html'])
    return bytes_sent, http_status


# noinspection PyUnusedLocal
async def api_config_callback(http, verb, args, reader, writer, request_headers=None):  # callback for '/api/config'
    if verb == 'GET':
        payload = read_config()
        # payload.pop('secret')  # do not return the secret
        response = json.dumps(payload).encode('utf-8')
        http_status = 200
        bytes_sent = http.send_simple_response(writer, http_status, http.CT_APP_JSON, response)
    elif verb == 'POST':
        config = read_config()
        dirty = False
        errors = False
        tcp_port = args.get('tcp_port')
        if tcp_port is not None:
            tcp_port_int = safe_int(tcp_port, -2)
            if 0 <= tcp_port_int <= 65535:
                config['tcp_port'] = tcp_port
                dirty = True
            else:
                errors = True
        web_port = args.get('web_port')
        if web_port is not None:
            web_port_int = safe_int(web_port, -2)
            if 0 <= web_port_int <= 65535:
                config['web_port'] = web_port
                dirty = True
            else:
                errors = True
        ssid = args.get('SSID')
        if ssid is not None:
            if 0 < len(ssid) < 64:
                config['SSID'] = ssid
                dirty = True
            else:
                errors = True
        secret = args.get('secret')
        if secret is not None:
            if 8 <= len(secret) < 32:
                config['secret'] = secret
                dirty = True
            else:
                errors = True
        ap_mode_arg = args.get('ap_mode')
        if ap_mode_arg is not None:
            ap_mode = True if ap_mode_arg == '1' else False
            config['ap_mode'] = ap_mode
            dirty = True
        dhcp_arg = args.get('dhcp')
        if dhcp_arg is not None:
            dhcp = True if dhcp_arg == 1 else False
            config['dhcp'] = dhcp
            dirty = True
        ip_address = args.get('ip_address')
        if ip_address is not None:
            config['ip_address'] = ip_address
            dirty = True
        netmask = args.get('netmask')
        if netmask is not None:
            config['netmask'] = netmask
            dirty = True
        gateway = args.get('gateway')
        if gateway is not None:
            config['gateway'] = gateway
            dirty = True
        dns_server = args.get('dns_server')
        if dns_server is not None:
            config['dns_server'] = dns_server
            dirty = True
        if not errors:
            if dirty:
                save_config(config)
            response = b'ok\r\n'
            http_status = 200
            bytes_sent = http.send_simple_response(writer, http_status, CT_TEXT_TEXT, response)
        else:
            response = b'parameter out of range\r\n'
            http_status = 400
            bytes_sent = http.send_simple_response(writer, http_status, CT_TEXT_TEXT, response)

    else:
        response = b'GET or PUT only.'
        http_status = 400
        bytes_sent = http.send_simple_response(writer, http_status, http.CT_TEXT_TEXT, response)
    return bytes_sent, http_status


# noinspection PyUnusedLocal
async def api_get_files_callback(http, verb, args, reader, writer, request_headers=None):
    if verb == 'GET':
        payload = os.listdir(http.content_dir)
        response = json.dumps(payload).encode('utf-8')
        http_status = 200
        bytes_sent = http.send_simple_response(writer, http_status, http.CT_APP_JSON, response)
    else:
        http_status = 400
        response = b'only GET permitted'
        bytes_sent = http.send_simple_response(writer, http_status, http.CT_APP_JSON, response)
    return bytes_sent, http_status


# noinspection PyUnusedLocal
async def api_upload_file_callback(http, verb, args, reader, writer, request_headers=None):
    if verb == 'POST':
        boundary = None
        request_content_type = request_headers.get('Content-Type') or ''
        if ';' in request_content_type:
            pieces = request_content_type.split(';')
            request_content_type = pieces[0]
            boundary = pieces[1].strip()
            if boundary.startswith('boundary='):
                boundary = boundary[9:]
        if request_content_type != http.CT_MULTIPART_FORM or boundary is None:
            response = b'multipart boundary or content type error'
            http_status = 400
        else:
            response = b'unhandled problem'
            http_status = 500
            request_content_length = int(request_headers.get('Content-Length') or '0')
            remaining_content_length = request_content_length
            start_boundary = http.HYPHENS + boundary
            end_boundary = start_boundary + http.HYPHENS
            state = http.MP_START_BOUND
            filename = None
            output_file = None
            writing_file = False
            more_bytes = True
            leftover_bytes = []
            while more_bytes:
                # print('waiting for read')
                buffer = await reader.read(BUFFER_SIZE)
                remaining_content_length -= len(buffer)
                if remaining_content_length == 0:  # < BUFFER_SIZE:
                    more_bytes = False
                if len(leftover_bytes) != 0:
                    buffer = leftover_bytes + buffer
                    leftover_bytes = []
                start = 0
                while start < len(buffer):
                    if state == http.MP_DATA:
                        if not output_file:
                            output_file = open(http.content_dir + 'uploaded_' + filename, 'wb')
                            writing_file = True
                        end = len(buffer)
                        for i in range(start, len(buffer) - 3):
                            if buffer[i] == 13 and buffer[i + 1] == 10 and buffer[i + 2] == 45 and \
                                    buffer[i + 3] == 45:
                                end = i
                                writing_file = False
                                break
                        if end == BUFFER_SIZE:
                            if buffer[-1] == 13:
                                leftover_bytes = buffer[-1:]
                                buffer = buffer[:-1]
                                end -= 1
                            elif buffer[-2] == 13 and buffer[-1] == 10:
                                leftover_bytes = buffer[-2:]
                                buffer = buffer[:-2]
                                end -= 2
                            elif buffer[-3] == 13 and buffer[-2] == 10 and buffer[-1] == 45:
                                leftover_bytes = buffer[-3:]
                                buffer = buffer[:-3]
                                end -= 3
                        output_file.write(buffer[start:end])
                        if not writing_file:
                            # print('closing file')
                            state = http.MP_END_BOUND
                            output_file.close()
                            output_file = None
                            response = f'Uploaded {filename} successfully'.encode('utf-8')
                            http_status = 201
                        start = end + 2
                    else:  # must be reading headers or boundary
                        line = ''
                        for i in range(start, len(buffer) - 1):
                            if buffer[i] == 13 and buffer[i + 1] == 10:
                                line = buffer[start:i].decode('utf-8')
                                start = i + 2
                                break
                        if state == http.MP_START_BOUND:
                            if line == start_boundary:
                                state = http.MP_HEADERS
                            else:
                                logging.error(f'expecting start boundary, got {line}', 'main:api_upload_file_callback')
                        elif state == http.MP_HEADERS:
                            if len(line) == 0:
                                state = http.MP_DATA
                            elif line.startswith('Content-Disposition:'):
                                pieces = line.split(';')
                                fn = pieces[2].strip()
                                if fn.startswith('filename="'):
                                    filename = fn[10:-1]
                                    if not valid_filename(filename):
                                        response = b'bad filename'
                                        http_status = 500
                                        more_bytes = False
                                        start = len(buffer)
                            # else:
                            #     print('processing headers, got ' + line)
                        elif state == http.MP_END_BOUND:
                            if line == end_boundary:
                                state = http.MP_START_BOUND
                            else:
                                logging.error(f'expecting end boundary, got {line}', 'main:api_upload_file_callback')
                        else:
                            http_status = 500
                            response = f'unmanaged state {state}'.encode('utf-8')
        bytes_sent = http.send_simple_response(writer, http_status, http.CT_TEXT_TEXT, response)
    else:
        response = b'PUT only.'
        http_status = 400
        bytes_sent = http.send_simple_response(writer, http_status, http.CT_TEXT_TEXT, response)
    return bytes_sent, http_status


# noinspection PyUnusedLocal
async def api_remove_file_callback(http, verb, args, reader, writer, request_headers=None):
    filename = args.get('filename')
    if valid_filename(filename) and filename not in DANGER_ZONE_FILE_NAMES:
        filename = http.content_dir + filename
        try:
            os.remove(filename)
            http_status = 200
            response = b'removed\r\n'
        except OSError as ose:
            http_status = 409
            response = str(ose).encode('utf-8')
    else:
        http_status = 409
        response = b'bad file name\r\n'
    bytes_sent = http.send_simple_response(writer, http_status, http.CT_APP_JSON, response)
    return bytes_sent, http_status


# noinspection PyUnusedLocal
async def api_rename_file_callback(http, verb, args, reader, writer, request_headers=None):
    filename = args.get('filename')
    newname = args.get('newname')
    if valid_filename(filename) and valid_filename(newname):
        filename = http.content_dir + filename
        newname = http.content_dir + newname
        try:
            os.remove(newname)
        except OSError:
            pass  # swallow exception.
        try:
            os.rename(filename, newname)
            http_status = 200
            response = b'renamed\r\n'
        except Exception as ose:
            http_status = 409
            response = str(ose).encode('utf-8')
    else:
        http_status = 409
        response = b'bad file name'
    bytes_sent = http.send_simple_response(writer, http_status, http.CT_APP_JSON, response)
    return bytes_sent, http_status


# noinspection PyUnusedLocal
async def api_restart_callback(http, verb, args, reader, writer, request_headers=None):
    global restart
    if upython:
        restart = True
        response = b'ok\r\n'
        http_status = 200
        bytes_sent = http.send_simple_response(writer, http_status, http.CT_TEXT_TEXT, response)
    else:
        http_status = 400
        response = b'not permitted except on PICO-W'
        bytes_sent = http.send_simple_response(writer, http_status, http.CT_APP_JSON, response)
    return bytes_sent, http_status


async def api_status_callback(http, verb, args, reader, writer, request_headers=None):  # '/api/kpa_status'
    payload = {'timestamp': get_timestamp(),
               'last_temperature': f'{last_temperature:3.1f}',
               'last_humidity': f'{last_humidity:3.1f}',
               'last_pressure': f'{last_pressure:5.2f}',
               't_trend': str(t_samples),
               'h_trend': str(h_samples),
               'p_trend': str(p_samples),
               }

    response = json.dumps(payload).encode('utf-8')
    http_status = 200
    bytes_sent = http.send_simple_response(writer, http_status, http.CT_APP_JSON, response)
    return bytes_sent, http_status


async def serve_serial_client(reader, writer):
    """
    send the data over a dumb connection
    """
    t0 = milliseconds()
    partner = writer.get_extra_info('peername')[0]
    print('\nclient connected from {}'.format(partner))
    buffer = []
    client_connected = True

    try:
        while client_connected:
            data = await reader.read(1)
            if data is None:
                break
            else:
                if len(data) == 1:
                    b = data[0]
                    if b == 10:  # line feed, get temperature
                        message = f'{get_timestamp()} {last_temperature} {last_humidity} {last_pressure}\r\n'.encode()
                        writer.write(message)
                    elif b == 81 or b == 113:  # q/Q exit
                        client_connected = False
                    await writer.drain()

        reader.close()
        writer.close()
        await writer.wait_closed()

    except Exception as ex:
        print('exception in serve_serial_client:', type(ex), ex)
    tc = milliseconds()
    print('client disconnected, elapsed time {:6.3f} seconds'.format((tc - t0) / 1000.0))


async def bme280_reader(verbosity=4):
    global last_temperature, last_humidity, last_pressure
    i2c = machine.I2C(1, scl=machine.Pin(27), sda=machine.Pin(26))
    result = array('f', (0.0, 0.0, 0.0))
    bme = bme280.BME280(i2c=i2c)
    divider_count = 1
    # sht = SHT30(i2c_id=1, scl_pin=27, sda_pin=26)

    while True:
        result = bme.read_compensated_data(result)
        tc = result[0]
        p = result[1]
        h = result[2]
        # tc, h = sht.measure()
        #p += 3507.67  # correction factor, unknown why at this time.
        p += 3625  # correction factor, unknown why at this time.
        hpa = p / 100.0

        tf = tc * 1.8 + 32.0  # make Fahrenheit for Americans
        inhg = p / 1000 * 0.295300  # make inches of mercury for Americans

        pp = int((hpa - 950) * 2)
        pt = int((tc + 40) * 2)
        ph = int(h * 2)

        last_temperature = round(tf, 1)
        last_pressure = round(inhg, 2)
        last_humidity = round(h, 1)
        if verbosity > 4:
            print(f'{hpa:7.2f} hPa')
            print(f'{get_timestamp()} ' +
                  f'temperature {last_temperature:5.1f}F, ' +
                  f'humidity {last_humidity:5.1f}%, ' +
                  f'pressure {last_pressure:5.2f} in. Hg')
            print(f'{pt}, {ph}, {pp}')

        divider_count -= 1
        if divider_count == 0:  # every 6 minutes, .1 hour
            divider_count = 6  # this is the number of minutes between samples.
            t_samples.add_sample(pt)
            h_samples.add_sample(ph)
            p_samples.add_sample(pp)

        await asyncio.sleep(60.0)
        #await asyncio.sleep(1.0)


async def main():
    global port, restart
    config = read_config()
    tcp_port = safe_int(config.get('tcp_port') or DEFAULT_TCP_PORT, DEFAULT_TCP_PORT)
    if tcp_port < 0 or tcp_port > 65535:
        tcp_port = DEFAULT_TCP_PORT
    web_port = safe_int(config.get('web_port') or DEFAULT_WEB_PORT, DEFAULT_WEB_PORT)
    if web_port < 0 or web_port > 65535:
        web_port = DEFAULT_WEB_PORT

    connected = True
    if upython:
        try:
            ip_address = connect_to_network(config)
            connected = ip_address is not None
        except Exception as ex:
            connected = False
            print(type(ex), ex)

    if upython:
        asyncio.create_task(morse_code_sender.morse_sender())

    if connected:
        ntp_time = ntp.get_ntp_time()
        if ntp_time is None:
            print('ntp time query failed.  clock may be inaccurate.')
        else:
            print('Got time from NTP: {}'.format(get_timestamp()))

        http_server.add_uri_callback('/', slash_callback)
        http_server.add_uri_callback('/api/config', api_config_callback)
        http_server.add_uri_callback('/api/get_files', api_get_files_callback)
        http_server.add_uri_callback('/api/upload_file', api_upload_file_callback)
        http_server.add_uri_callback('/api/remove_file', api_remove_file_callback)
        http_server.add_uri_callback('/api/rename_file', api_rename_file_callback)
        http_server.add_uri_callback('/api/restart', api_restart_callback)
        http_server.add_uri_callback('/api/status', api_status_callback)

        print('Starting web service on port {}'.format(web_port))
        web_server = asyncio.create_task(asyncio.start_server(http_server.serve_http_client, '0.0.0.0', web_port))
        print('Starting tcp service on port {}'.format(tcp_port))
        tcp_server = asyncio.create_task(asyncio.start_server(serve_serial_client, '0.0.0.0', tcp_port))
    else:
        print('no network connection')

    if upython:
        # asyncio.create_task(sht30_reader())
        asyncio.create_task(bme280_reader(5))

    if upython:
        last_pressed = button.value() == 0
    else:
        last_pressed = False

    while True:
        if upython:
            await asyncio.sleep(0.25)
            pressed = button.value() == 0
            if not last_pressed and pressed:  # look for activating edge
                ap_mode = config.get('ap_mode') or False
                ap_mode = not ap_mode
                config['ap_mode'] = ap_mode
                save_config(config)
                restart = True
            last_pressed = pressed

            if restart:
                machine.soft_reset()
        else:
            await asyncio.sleep(10.0)


if __name__ == '__main__':
    print('starting')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('bye')
    finally:
        asyncio.new_event_loop()  # why? to drain?
    print('done')
