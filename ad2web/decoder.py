# -*- coding: utf-8 -*-

import os
import sys
import time
import traceback
import threading

from socketio import socketio_manage
from socketio.namespace import BaseNamespace
from socketio.mixins import BroadcastMixin
from socketio.server import SocketIOServer

from flask import Blueprint, Response, request, g, current_app
import jsonpickle

from OpenSSL import SSL
from alarmdecoder import AlarmDecoder
from alarmdecoder.devices import SocketDevice, SerialDevice
from alarmdecoder.util import NoDeviceError, CommError

from .log.models import EventLogEntry
from .log.constants import *
from .extensions import db
from .notifications import NotificationFactory
from .zones import Zone
from .settings.models import Setting
from .certificate.models import Certificate
from .updater import Updater

CRITICAL_EVENTS = [POWER_CHANGED, ALARM, BYPASS, ARM, DISARM, ZONE_FAULT, \
                    ZONE_RESTORE, FIRE, PANIC]

EVENTS = {
    ARM: 'on_arm',
    DISARM: 'on_disarm',
    POWER_CHANGED: 'on_power_changed',
    ALARM: 'on_alarm',
    FIRE: 'on_fire',
    BYPASS: 'on_bypass',
    BOOT: 'on_boot',
    CONFIG_RECEIVED: 'on_config_received',
    ZONE_FAULT: 'on_zone_fault',
    ZONE_RESTORE: 'on_zone_restore',
    LOW_BATTERY: 'on_low_battery',
    PANIC: 'on_panic',
    RELAY_CHANGED: 'on_relay_changed'
}

EVENT_MESSAGES = {
    ARM: 'The alarm was armed.',
    DISARM: 'The alarm was disarmed.',
    POWER_CHANGED: 'Power status has changed to {status}.',
    ALARM: 'Alarm is triggered!',
    FIRE: 'There is a fire!',
    BYPASS: 'A zone has been bypassed.',
    BOOT: 'The AlarmDecoder has finished booting.',
    CONFIG_RECEIVED: 'AlarmDecoder has been configured.',
    ZONE_FAULT: '{zone_name} ({zone}) has been faulted.',
    ZONE_RESTORE: '{zone_name} ({zone}) has been restored.',
    LOW_BATTERY: 'Low battery detected.',
    PANIC: 'Panic!',
    RELAY_CHANGED: 'A relay has changed.'
}

decodersocket = Blueprint('sock', __name__, url_prefix='/socket.io')

def create_decoder_socket(app):
    return SocketIOServer(('', 5000), app, resource="socket.io")

class Decoder(object):
    def __init__(self, app, websocket):
        with app.app_context():
            self.app = app
            self.websocket = websocket
            self.device = None
            self.updater = Updater()
            self.updates = {}

            self.trigger_reopen_device = False
            self.trigger_restart = False

            self._last_message = None
            self._device_baudrate = 115200
            self._device_type = None
            self._device_location = None
            self._event_thread = DecoderThread(self)
            self._version_thread = VersionChecker(self)

    def start(self):
        self._event_thread.start()
        self._version_thread.start()

    def stop(self, restart=False):
        self.app.logger.info('Stopping service..')

        self.close()

        self._event_thread.stop()
        self._version_thread.stop()

        if restart:
            try:
                self._event_thread.join(5)
                self._version_thread.join(5)
            except RuntimeError:
                pass

        self.websocket.stop()

        if restart:
            self.app.logger.info('Restarting service..')
            os.execv(sys.executable, [sys.executable] + sys.argv)

    def init(self):
        with self.app.app_context():
            device_type = Setting.get_by_name('device_type').value

            if device_type:
                self.trigger_reopen_device = True

    def open(self):
        with self.app.app_context():
            self._device_type = Setting.get_by_name('device_type').value
            self._device_location = Setting.get_by_name('device_location').value

            if self._device_type:
                interface = ('localhost', 10000)
                use_ssl = False
                devicetype = SocketDevice

                # Set up device interfaces based on our location.
                if self._device_location == 'local':
                    devicetype = SerialDevice
                    interface = Setting.get_by_name('device_path').value
                    self._device_baudrate = Setting.get_by_name('device_baudrate').value

                elif self._device_location == 'network':
                    interface = (Setting.get_by_name('device_address').value, Setting.get_by_name('device_port').value)
                    use_ssl = Setting.get_by_name('use_ssl', False).value

                # Create and open the device.
                try:
                    device = devicetype(interface=interface)
                    if use_ssl:
                        ca_cert = Certificate.query.filter_by(name='AlarmDecoder CA').one()
                        internal_cert = Certificate.query.filter_by(name='AlarmDecoder Internal').one()

                        device.ssl = True
                        device.ssl_ca = ca_cert.certificate_obj
                        device.ssl_certificate = internal_cert.certificate_obj
                        device.ssl_key = internal_cert.key_obj

                    self.device = AlarmDecoder(device)
                    self.bind_events(self.websocket, self.device)
                    self.device.open(baudrate=self._device_baudrate)

                except NoDeviceError, err:
                    self.app.logger.warning('Open failed: %s', err[0], exc_info=True)
                    raise

                except SSL.Error, err:
                    source, fn, message = err[0][0]
                    self.app.logger.warning('SSL connection failed: %s - %s', fn, message, exc_info=True)
                    raise

    def close(self):
        if self.device:
            self.device.close()

    def bind_events(self, appsocket, decoder):
        build_event_handler = lambda ftype: lambda sender, *args, **kwargs: self._handle_event(ftype, sender, *args, **kwargs)
        build_message_handler = lambda ftype: lambda sender, *args, **kwargs: self._on_message(ftype, sender, *args, **kwargs)

        self.device.on_message += build_message_handler('panel')
        self.device.on_lrr_message += build_message_handler('lrr')
        self.device.on_rfx_message += build_message_handler('rfx')
        self.device.on_expander_message += build_message_handler('exp')

        self.device.on_open += self._on_device_open
        self.device.on_close += self._on_device_close

        # Bind the event handler to all of our events.
        for event, device_event_name in EVENTS.iteritems():
            device_handler = getattr(self.device, device_event_name)
            device_handler += build_event_handler(event)

    def _on_device_open(self, sender):
        self.app.logger.info('AlarmDecoder device was opened.')

        self.broadcast('device_open')
        self.trigger_reopen_device = False

    def _on_device_close(self, sender):
        self.app.logger.info('AlarmDecoder device was closed.')

        self.broadcast('device_close')
        self.trigger_reopen_device = True

    def _on_message(self, ftype, sender, *args, **kwargs):
        try:
            self.broadcast('message', { 'message': kwargs.get('message', None), 'message_type': ftype } )

        except Exception, err:
            self.app.logger.error('Error while broadcasting message.', exc_info=True)

    def _handle_event(self, ftype, sender, *args, **kwargs):
        try:
            self._last_message = time.time()

            with self.app.app_context():
                if 'zone' in kwargs:
                    zone_name = Zone.get_name(kwargs['zone'])
                    kwargs['zone_name'] = zone_name if zone_name else '<unnamed>'

                event_message = EVENT_MESSAGES[ftype].format(**kwargs)
                if ftype in CRITICAL_EVENTS:
                    for id in NotificationFactory.notifications():
                        notifier = NotificationFactory.create(id)
                        notifier.send('AlarmDecoder Event: {0}'.format(event_message))

                db.session.add(EventLogEntry(type=ftype, message=event_message))
                db.session.commit()

            self.broadcast('event', kwargs)

        except Exception, err:
            self.app.logger.error('Error while broadcasting event.', exc_info=True)

    def broadcast(self, channel, data={}):
        obj = jsonpickle.encode(data, unpicklable=False)
        packet = self._make_packet(channel, obj)

        self._broadcast_packet(packet)

    def _broadcast_packet(self, packet):
        for session, sock in self.websocket.sockets.iteritems():
            sock.send_packet(packet)

    def _make_packet(self, channel, data):
        return dict(type='event', name=channel, args=data, endpoint='/alarmdecoder')

class DecoderThread(threading.Thread):
    """
    Worker thread for handling device events, specifically device reconnection.
    """

    TIMEOUT = 5

    def __init__(self, decoder):
        threading.Thread.__init__(self)
        self._decoder = decoder
        self._running = False

    def stop(self):
        """
        Stops the running thread.
        """
        self._running = False

    def run(self):
        """
        The actual read process.
        """
        self._running = True

        while self._running:
            with self._decoder.app.app_context():
                try:
                    # Handle reopen events
                    if self._decoder.trigger_reopen_device:
                        self._decoder.app.logger.info('Attempting to reconnect to the AlarmDecoder')
                        try:
                            self._decoder.open()
                        except NoDeviceError, err:
                            self._decoder.app.logger.error('Device not found: {0}'.format(err[0]))

                    # Handle service restart events
                    if self._decoder.trigger_restart:
                        self._decoder.app.logger.info('Restarting service..')
                        self._decoder.stop(restart=True)

                    time.sleep(self.TIMEOUT)

                except Exception, err:
                    self._decoder.app.logger.error('Error in DecoderThread: {0}'.format(err), exc_info=True)

class VersionChecker(threading.Thread):
    TIMEOUT = 60 * 10

    def __init__(self, decoder):
        threading.Thread.__init__(self)
        self._decoder = decoder
        self._updater = decoder.updater
        self._running = False

    def stop(self):
        self._running = False

    def run(self):
        self._running = True

        while self._running:
            self._decoder.updates = self._updater.check_updates()

            time.sleep(self.TIMEOUT)

class DecoderNamespace(BaseNamespace, BroadcastMixin):
    def initialize(self):
        self._alarmdecoder = self.request

    def on_keypress(self, key):
        with self._alarmdecoder.app.app_context():
            try:
                if key == 1:
                    self._alarmdecoder.device.send(AlarmDecoder.KEY_F1)
                elif key == 2:
                    self._alarmdecoder.device.send(AlarmDecoder.KEY_F2)
                elif key == 3:
                    self._alarmdecoder.device.send(AlarmDecoder.KEY_F3)
                elif key == 4:
                    self._alarmdecoder.device.send(AlarmDecoder.KEY_F4)
                elif key == 5:
                    self._alarmdecoder.device.send(AlarmDecoder.KEY_PANIC)
                else:
                    self._alarmdecoder.device.send(key)

            except CommError, err:
                self._alarmdecoder.app.logger.error('Error sending keypress to device', exc_info=True)

    def on_test(self, *args):
        with self._alarmdecoder.app.app_context():
            try:
                self._test_open()
                time.sleep(0.5)
                self._test_config()
                self._test_send()
                self._test_receive()

            except Exception:
                current_app.logger.error('Error running device tests.', exc_info=True)

    def _test_open(self):
        results = 'PASS'
        details = ''

        try:
            self._alarmdecoder.close()
            self._alarmdecoder.open()

        except NoDeviceError, err:
            results = 'FAIL'
            details = '{0}: {1}'.format(err[0], err[1][1])
            current_app.logger.error('Error while testing device open.', exc_info=True)

        except Exception, err:
            results = 'FAIL'
            details = 'Failed to open the device: {0}'.format(err)
            current_app.logger.error('Error while testing device open.', exc_info=True)

        finally:
            self._alarmdecoder.broadcast('test', {'test': 'open', 'results': results, 'details': details})

    def _test_config(self):
        def on_config_received(device):
            timer.cancel()
            self._alarmdecoder.broadcast('test', {'test': 'config', 'results': 'PASS', 'details': ''})
            if on_config_received in self._alarmdecoder.device.on_config_received:
                self._alarmdecoder.device.on_config_received.remove(on_config_received)

        def on_timeout():
            self._alarmdecoder.broadcast('test', {'test': 'config', 'results': 'TIMEOUT', 'details': 'Test timed out.'})
            if on_config_received in self._alarmdecoder.device.on_config_received:
                self._alarmdecoder.device.on_config_received.remove(on_config_received)

        timer = threading.Timer(10, on_timeout)
        timer.start()

        try:
            panel_mode = Setting.get_by_name('panel_mode')
            keypad_address = Setting.get_by_name('keypad_address')
            address_mask = Setting.get_by_name('address_mask')
            lrr_enabled = Setting.get_by_name('lrr_enabled')
            zone_expanders = Setting.get_by_name('emulate_zone_expanders')
            relay_expanders = Setting.get_by_name('emulate_relay_expanders')
            deduplicate = Setting.get_by_name('deduplicate')

            zx = [x == u'True' for x in zone_expanders.value.split(',')]
            rx = [x == u'True' for x in relay_expanders.value.split(',')]

            self._alarmdecoder.device.mode = panel_mode.value
            self._alarmdecoder.device.address = keypad_address.value
            self._alarmdecoder.device.address_mask = int(address_mask.value, 16)
            self._alarmdecoder.device.emulate_zone = zx
            self._alarmdecoder.device.emulate_relay = rx
            self._alarmdecoder.device.emulate_lrr = lrr_enabled.value
            self._alarmdecoder.device.deduplicate = deduplicate.value

            self._alarmdecoder.device.on_config_received += on_config_received
            self._alarmdecoder.device.save_config()

        except Exception, err:
            timer.cancel()
            if on_config_received in self._alarmdecoder.device.on_config_received:
                self._alarmdecoder.device.on_config_received.remove(on_config_received)

            self._alarmdecoder.broadcast('test', {'test': 'config', 'results': 'FAIL', 'details': 'There was an error sending the command to the device.'})
            current_app.logger.error('Error while testing device config.', exc_info=True)

    def _test_send(self):
        def on_sending_received(device, status, message):
            timer.cancel()
            if on_sending_received in self._alarmdecoder.device.on_sending_received:
                self._alarmdecoder.device.on_sending_received.remove(on_sending_received)

            results, details = 'PASS', ''
            if status != True:
                results, details = 'FAIL', 'Check wiring and that the correct keypad address is being used.'

            self._alarmdecoder.broadcast('test', {'test': 'send', 'results': results, 'details': details})

        def on_timeout():
            self._alarmdecoder.broadcast('test', {'test': 'send', 'results': 'TIMEOUT', 'details': 'Test timed out.'})
            if on_sending_received in self._alarmdecoder.device.on_sending_received:
                self._alarmdecoder.device.on_sending_received.remove(on_sending_received)

        timer = threading.Timer(10, on_timeout)
        timer.start()

        try:
            self._alarmdecoder.device.on_sending_received += on_sending_received
            self._alarmdecoder.device.send("*\r")

        except Exception, err:
            timer.cancel()
            if on_sending_received in self._alarmdecoder.device.on_sending_received:
                self._alarmdecoder.device.on_sending_received.remove(on_sending_received)

            self._alarmdecoder.broadcast('test', {'test': 'send', 'results': 'FAIL', 'details': 'There was an error sending the command to the device.'})
            current_app.logger.error('Error while testing keypad communication.', exc_info=True)

    def _test_receive(self):
        def on_message(device, message):
            timer.cancel()
            if on_message in self._alarmdecoder.device.on_message:
                self._alarmdecoder.device.on_message.remove(on_message)

            self._alarmdecoder.broadcast('test', {'test': 'recv', 'results': 'PASS', 'details': ''})

        def on_timeout():
            self._alarmdecoder.broadcast('test', {'test': 'recv', 'results': 'TIMEOUT', 'details': 'Test timed out.'})
            if on_message in self._alarmdecoder.device.on_message:
                self._alarmdecoder.device.on_message.remove(on_message)

        timer = threading.Timer(10, on_timeout)
        timer.start()

        try:
            self._alarmdecoder.device.on_message += on_message
            self._alarmdecoder.device.send("*\r")

        except Exception, err:
            timer.cancel()
            if on_message in self._alarmdecoder.device.on_message:
                self._alarmdecoder.device.on_message.remove(on_message)

            self._alarmdecoder.broadcast('test', {'test': 'recv', 'results': 'FAIL', 'details': 'There was an error sending the command to the device.'})
            current_app.logger.error('Error while testing keypad communication.', exc_info=True)

@decodersocket.route('/<path:remaining>')
def handle_socketio(remaining):
    try:
        socketio_manage(request.environ, {'/alarmdecoder': DecoderNamespace}, g.alarmdecoder)

    except Exception, err:
        current_app.logger.error("Exception while handling socketio connection", exc_info=True)

    return Response()
