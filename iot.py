"""WolkAbout Python Connector library for connecting Zerynth devices to `WolkAbout IoT Platform <https://demo.wolkabout.com/>`_."""
#   Copyright 2018 WolkAbout Technology s.r.o.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
import timers

from wolkabout.iot.wolk import wolkabout_protocol_message_factory as wapmf
from wolkabout.iot.wolk import wolkabout_protocol_message_deserializer as wapmd
from wolkabout.iot.wolk import zerynth_message_queue as zmq
from wolkabout.iot.wolk import mqtt_connectivity_service as mcs
from wolkabout.iot.wolk.model import sensor_reading
from wolkabout.iot.wolk.model import alarm
from wolkabout.iot.wolk.model import actuator_status

new_exception(InterfaceNotProvided, Exception)  # noqa


@c_native("_totuple", ["csrc/tuple_ifc.c"], [])  # noqa
def tuple(mlist):  # noqa
    pass


debug_mode = False


def _print_d(*args):
    if debug_mode:
        print(*args)


class Wolk:
    """Wrapper for the whole library."""

    def __init__(
        self,
        device,
        host="api-demo.wolkabout.com",
        port=1883,
        actuation_handler=None,
        actuator_status_provider=None,
        configuration_handler=None,
        configuration_provider=None,
        message_queue_size=100,
        keep_alive_enabled=True,
    ):
        """
        Wrap together all functionality.

        :param device: Device containing key, password and actuator references
        :type device: Device
        :param host: Address of the MQTT broker of the Platform - defaults to demo instance
        :type host: str, optional
        :param port: Port of the MQTT broker
        :type port: int, optional
        :param actuation_handler: Actuation handler
        :type actuation_handler: None, optional
        :param actuator_status_provider: Actuator status provider
        :type actuator_status_provider: None, optional
        :param configuration_handler: Configuration handler
        :type configuration_handler: None, optional
        :param configuration_provider: Configuration provider
        :type configuration_provider: None, optional
        :param message_queue_size: Number of reading to store in memory
        :type message_queue_size: int, optional
        :param keep_alive_enabled: Periodically publish keep alive message
        :type keep_alive_enabled: bool, optional
        """
        self.device = device
        self.message_factory = wapmf.WolkAboutProtocolMessageFactory(device.key)
        self.message_deserializer = wapmd.WolkAboutProtocolMessageDeserializer(device)
        self.message_queue = zmq.ZerynthMessageQueue(message_queue_size)
        self.connectivity_service = mcs.MQTTConnectivityService(
            device, self.message_deserializer.get_inbound_topics(), host, port
        )
        self.connectivity_service.set_inbound_message_listener(self._on_inbound_message)
        self.actuation_handler = actuation_handler
        self.actuator_status_provider = actuator_status_provider
        self.configuration_handler = configuration_handler
        self.configuration_provider = configuration_provider
        self.keep_alive_enabled = keep_alive_enabled
        self.keep_alive_service = None
        self.last_platform_timestamp = None

        if device.actuator_references and (
            actuation_handler is None or actuator_status_provider is None
        ):
            raise InterfaceNotProvided

    def connect(self):
        """Connect to the Platform."""
        self.connectivity_service.connect()
        if self.keep_alive_enabled:
            self.keep_alive_service = timers.timer()
            self.keep_alive_service.interval(60000, self._send_keep_alive)
            self.keep_alive_service.start()

    def disconnect(self):
        """Disconnect from the Platform."""
        self.connectivity_service.disconnect()
        if self.keep_alive_enabled:
            self.keep_alive_service.stop()

    def _send_keep_alive(self):
        print("called ping pub")
        message = self.message_factory.make_from_keep_alive_message()
        self.connectivity_service.publish(message)

    def add_sensor_reading(self, reference, value, timestamp=None):
        """
        Add a sensor reading into storage.

        :param reference: The reference of the sensor
        :type reference: str
        :param value: The value of the sensor reading
        :type value: bool, int, float, str or tuple of previous types
        :param timestamp: (optional) Unix timestamp - if not provided, Platform will assign one
        :type timestamp: int
        """
        reading = sensor_reading.SensorReading(reference, value, timestamp)
        message = self.message_factory.make_from_sensor_reading(reading)
        self.message_queue.put(message)

    def add_alarm(self, reference, active, timestamp=None):
        """
        Add an alarm event into storage.

        :param reference: The reference of the alarm
        :type reference: str
        :param active: Current state of the alarm
        :type active: bool
        :param timestamp: (optional) Unix timestamp - if not provided, Platform will assign one
        :type timestamp: int
        """
        alarm_event = alarm.Alarm(reference, active, timestamp)
        message = self.message_factory.make_from_alarm(alarm_event)
        self.message_queue.put(message)

    def publish(self):
        """Publish all currently stored messages to the Platform."""
        while True:
            message = self.message_queue.peek()
            if message is None:
                break
            if self.connectivity_service.publish(message) is True:
                self.message_queue.get()

    def publish_actuator_status(self, reference):
        """
        Publish the current actuator status to the Platform.

        :param reference: The reference of the actuator
        :type reference: str
        """
        if self.actuator_status_provider is None:
            return

        state, value = self.actuator_status_provider(reference)
        status = actuator_status.ActuatorStatus(reference, state, value)
        message = self.message_factory.make_from_actuator_status(status)

        if not self.connectivity_service.publish(message):
            self.message_queue.put(message)

    def publish_configuration(self):
        """Publish the current device configuration to the Platform."""
        if self.configuration_handler is None:
            return

        configuration = self.configuration_provider()
        message = self.message_factory.make_from_configuration(configuration)
        if not self.connectivity_service.publish(message):
            self.message_queue.put(message)

    def request_timestamp(self):
        """
        Return last received Platform timestamp.

        If keep alive service is not enabled, this will always be None.

        :return: UTC timestamp in milliseconds or None
        :rtype: int, None
        """
        return self.last_platform_timestamp

    def _on_inbound_message(self, message):
        """
        Handle inbound messages.

        .. note:: Pass this function to the implementation of ConnectivityService

        :param message: The message received from the Platform
        :type message: Message
        """
        if self.message_deserializer.is_actuation_command(message):

            if not self.actuation_handler or not self.actuator_status_provider:
                return

            actuation = self.message_deserializer.parse_actuator_command(message)
            self.actuation_handler(actuation.reference, actuation.value)
            self.publish_actuator_status(actuation.reference)
            return

        if self.message_deserializer.is_configuration_command(message):

            if not self.configuration_provider or not self.configuration_handler:
                return

            configuration = self.message_deserializer.parse_configuration_command(
                message
            )
            self.configuration_handler(configuration)
            self.publish_configuration()
            return

        if self.message_deserializer.is_keep_alive_response(message):
            self.last_platform_timestamp = self.message_deserializer.parse_keep_alive_response(
                message
            )


class Device:
    """Device model."""

    def __init__(self, key, password, actuator_references=None):
        """
        Device identified by key and password, and a list of actuator references.

        :param key: Username used to connect to the platform
        :type key: str
        :param password: Password used to authenticate the connection
        :type password: str
        :param actuator_references: List of device's actuator references
        :type actuator_references: List[str]
        """
        self.key = key
        self.password = password
        self.actuator_references = actuator_references


# "Enum" of actuator states
ACTUATOR_STATE_READY = "READY"
ACTUATOR_STATE_BUSY = "BUSY"
ACTUATOR_STATE_ERROR = "ERROR"

# "Enum" of version number
VERSION_MAJOR = 2
VERSION_MINOR = 0
VERSION_PATCH = 0
