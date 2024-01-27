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
import time
from array import array

from http_server import (HttpServer,
                         api_rename_file_callback,
                         api_remove_file_callback,
                         api_upload_file_callback,
                         api_get_files_callback)
from morse_code import MorseCode
from ntp import get_ntp_time
from utils import milliseconds, upython, safe_int
from picow_network import connect_to_network

if upython:
    import machine
    import uasyncio as asyncio
    import bme280_float as bme280
    import micro_logging as logging
else:
    import asyncio
    import logging

    class Machine(object):
        """
        fake micropython Machine to make PyCharm happier.
        """

        @staticmethod
        def soft_reset():
            logging.warning('Machine.soft_reset()', 'main:Machine:soft_reset()')

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

            def off(self):
                self.value = 0

            def value(self) -> int:
                return self.value

        class I2C(object):
            def __init__(self, id, sda, scl):
                self.id = id
                self.sda = sda
                self.scl = scl

    machine = Machine()

onboard = machine.Pin('LED', machine.Pin.OUT, value=1)  # turn on right away
# blinky = machine.Pin(2, machine.Pin.OUT, value=0)  # status LED
button = machine.Pin(3, machine.Pin.IN, machine.Pin.PULL_UP)


CONFIG_FILE = 'data/config.json'
CONTENT_DIR = 'content/'

DEFAULT_SECRET = 'barometer'
DEFAULT_SSID = 'bme280'
DEFAULT_TCP_PORT = 73
DEFAULT_WEB_PORT = 80

# globals...
last_temperature = 0
last_humidity = 0
last_pressure = 0
restart = False
port = None
http_server = HttpServer(content_dir=CONTENT_DIR)
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
        logging.error(f'failed to load configuration:  {type(ex)}, {ex}', 'main:read_config()')
    return config


def save_config(config):
    with open(CONFIG_FILE, 'w') as config_file:
        json.dump(config, config_file)


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
            bytes_sent = http.send_simple_response(writer, http_status, http.CT_TEXT_TEXT, response)
        else:
            response = b'parameter out of range\r\n'
            http_status = 400
            bytes_sent = http.send_simple_response(writer, http_status, http.CT_TEXT_TEXT, response)

    else:
        response = b'GET or PUT only.'
        http_status = 400
        bytes_sent = http.send_simple_response(writer, http_status, http.CT_TEXT_TEXT, response)
    return bytes_sent, http_status


## noinspection PyUnusedLocal
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
    logging.info(f'client connected from {partner}', 'main:serve_serial_client')
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
                        payload = {'timestamp': get_timestamp(),
                                   'last_temperature': f'{last_temperature:3.1f}',
                                   'last_humidity': f'{last_humidity:3.1f}',
                                   'last_pressure': f'{last_pressure:5.2f}',
                                   't_trend': str(t_samples),
                                   'h_trend': str(h_samples),
                                   'p_trend': str(p_samples),
                                   }
                        response = (json.dumps(payload) + '\n').encode('utf-8')
                        writer.write(response)
                    elif b == 4 or b == 26 or b == 81 or b == 113:  # ^D/^z/q/Q exit
                        client_connected = False
                    await writer.drain()

        reader.close()
        writer.close()
        await writer.wait_closed()

    except Exception as ex:
        logging.error(f'exception in serve_serial_client: {type(ex)}, {ex}', 'main:serve_serial_client')
    tc = milliseconds()
    logging.info(f'client disconnected, elapsed time {(tc - t0) / 1000.0:6.3f} seconds', 'main:serve_serial_client')


async def bme280_reader(bme):
    global last_temperature, last_humidity, last_pressure
    result = array('f', (0.0, 0.0, 0.0))
    divider_count = 1

    while True:
        result = bme.read_compensated_data(result)
        tc = result[0]
        p = result[1]
        h = result[2]
        p += 3625  # correction factor, unknown why at this time.
        hpa = p / 100.0

        tf = tc * 1.8 + 32.0  # make Fahrenheit for Americans
        inhg = p / 1000 * 0.295300  # make inches of mercury for Americans

        last_temperature = round(tf, 1)
        last_pressure = round(inhg, 2)
        last_humidity = round(h, 1)
        logging.info(f'{hpa:7.2f} hPa', 'main:bme280_reader')
        logging.info(f'{get_timestamp()} temperature {last_temperature:5.1f}F, ' +
                     f'humidity {last_humidity:5.1f}%, pressure {last_pressure:5.2f} in. Hg', 'main:bme280_reader')

        divider_count -= 1
        if divider_count == 0:  # every 6 minutes, .1 hour
            divider_count = 6  # this is the number of minutes between samples.
            # scale samples
            pp = int((hpa - 950) * 2)  # pressure 950 - 1077 ( in 1/2 hpa intervals )
            pt = int((tc + 30) * 3)  # -30 -> 55c == -22 -> 131f in 1/3 degree C intervals
            if pt < 0:
                pt = 0
            elif pt > 254:
                pt = 254
            ph = int(h * 2.54)  # 0-100% as 0 - 254
            logging.info(f'collecting samples {pt}, {ph}, {pp}', 'main:bme280_reader')

            t_samples.add_sample(pt)
            h_samples.add_sample(ph)
            p_samples.add_sample(pp)

        await asyncio.sleep(60.0)


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
            ip_address = connect_to_network(config, DEFAULT_SSID, DEFAULT_SECRET, morse_code_sender)
            connected = ip_address is not None
        except Exception as ex:
            connected = False
            logging.error(f'Network connection failed, {type(ex)}, {ex}', 'main:main')

    if upython:
        morse_task = asyncio.create_task(morse_code_sender.morse_sender())

    if connected:
        ntp_time = get_ntp_time()
        if ntp_time is None:
            logging.error('ntp time query failed.  clock may be inaccurate.', 'main:main')
        else:
            logging.info(f'Got time from NTP: {get_timestamp()}', 'main:main')

        http_server.add_uri_callback('/', slash_callback)
        http_server.add_uri_callback('/api/config', api_config_callback)
        http_server.add_uri_callback('/api/get_files', api_get_files_callback)
        http_server.add_uri_callback('/api/upload_file', api_upload_file_callback)
        http_server.add_uri_callback('/api/remove_file', api_remove_file_callback)
        http_server.add_uri_callback('/api/rename_file', api_rename_file_callback)
        http_server.add_uri_callback('/api/restart', api_restart_callback)
        http_server.add_uri_callback('/api/status', api_status_callback)

        logging.info(f'Starting web service on port {web_port}', 'main:main')
        web_server = asyncio.create_task(asyncio.start_server(http_server.serve_http_client, '0.0.0.0', web_port))
        logging.info(f'Starting tcp service on port {tcp_port}', 'main:main')
        tcp_server = asyncio.create_task(asyncio.start_server(serve_serial_client, '0.0.0.0', tcp_port))
    else:
        logging.error('no network connection', 'main:main')

    if upython:
        i2c = machine.I2C(1, scl=machine.Pin(27), sda=machine.Pin(26))
        bme280_device = None
        try:
            bme280_device = bme280.BME280(i2c=i2c)
        except Exception as exc:
            logging.error(f'Cannot find bme280 sensor! {exc}', 'main:main')
            bme280_device = None

        if bme280_device is not None:
            bme280_task = asyncio.create_task(bme280_reader(bme280_device))

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
    # logging.loglevel = logging.DEBUG
    logging.loglevel = logging.INFO  # DEBUG
    logging.info('starting', 'main:__main__')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info('KeyboardInterrupt -- bye bye', 'main:__main__')
    finally:
        asyncio.new_event_loop()
    logging.info('done', 'main:__main__')
