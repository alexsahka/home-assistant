"""
homeassistant.remote
~~~~~~~~~~~~~~~~~~~~

A module containing drop in replacements for core parts that will interface
with a remote instance of home assistant.

If a connection error occurs while communicating with the API a
HomeAssistantError will be raised.
"""

import threading
import logging
import json
import enum
import urllib.parse

import requests

import homeassistant as ha

SERVER_PORT = 8123

URL_API = "/api/"
URL_API_STATES = "/api/states"
URL_API_STATES_ENTITY = "/api/states/{}"
URL_API_EVENTS = "/api/events"
URL_API_EVENTS_EVENT = "/api/events/{}"
URL_API_SERVICES = "/api/services"
URL_API_SERVICES_SERVICE = "/api/services/{}/{}"
URL_API_EVENT_FORWARD = "/api/event_forwarding"

METHOD_GET = "get"
METHOD_POST = "post"


class APIStatus(enum.Enum):
    """ Represents API status. """

    OK = "ok"
    INVALID_PASSWORD = "invalid_password"
    CANNOT_CONNECT = "cannot_connect"
    UNKNOWN = "unknown"

    def __str__(self):
        return self.value


class API(object):
    """ Object to pass around Home Assistant API location and credentials. """
    # pylint: disable=too-few-public-methods

    def __init__(self, host, api_password, port=None):
        self.host = host
        self.port = port or SERVER_PORT
        self.api_password = api_password
        self.base_url = "http://{}:{}".format(host, self.port)
        self.status = None

    def validate_api(self, force_validate=False):
        if self.status is None or force_validate:
            self.status = validate_api(self)

        return self.status == APIStatus.OK

    def __call__(self, method, path, data=None):
        """ Makes a call to the Home Assistant api. """
        data = data or {}
        data['api_password'] = self.api_password

        url = urllib.parse.urljoin(self.base_url, path)

        try:
            if method == METHOD_GET:
                return requests.get(url, params=data)
            else:
                return requests.request(method, url, data=data)

        except requests.exceptions.ConnectionError:
            logging.getLogger(__name__).exception("Error connecting to server")
            raise ha.HomeAssistantError("Error connecting to server")


class HomeAssistant(ha.HomeAssistant):
    """ Home Assistant that forwards work. """
    # pylint: disable=super-init-not-called

    def __init__(self, remote_api, local_api=None):
        if not remote_api.validate_api():
            raise ha.HomeAssistantError(
                "Remote API not valid: {}".format(remote_api.status))

        self.remote_api = remote_api
        self.local_api = local_api

        self._pool = pool = ha.create_worker_pool()

        self.bus = EventBus(remote_api, pool)
        self.services = ha.ServiceRegistry(self.bus, pool)
        self.states = StateMachine(self.bus, self.remote_api)

    def start(self):
        # If there is no local API setup but we do want to connect with remote
        # We create a random password and set up a local api
        if self.local_api is None:
            import homeassistant.components.http as http
            import random

            http.setup(self, '%030x'.format(random.randrange(16**30)))

        ha.Timer(self)

        # Setup that events from remote_api get forwarded to local_api
        connect_remote_events(self.remote_api, self.local_api)

        self.bus.fire(ha.EVENT_HOMEASSISTANT_START,
                      origin=ha.EventOrigin.remote)


class EventBus(ha.EventBus):
    """ EventBus implementation that forwards fire_event to remote API. """

    def __init__(self, api, pool=None):
        super().__init__(pool)
        self._api = api

    def fire(self, event_type, event_data=None, origin=ha.EventOrigin.local):
        """ Forward local events to remote target,
            handles remote event as usual. """
        # All local events that are not TIME_CHANGED are forwarded to API
        if origin == ha.EventOrigin.local and \
           event_type != ha.EVENT_TIME_CHANGED:

            fire_event(self._api, event_type, event_data)

        else:
            super().fire(event_type, event_data, origin)


class EventForwarder(object):
    """ Listens for events and forwards to specified APIs. """

    def __init__(self, hass, restrict_origin=None):
        self.hass = hass
        self.restrict_origin = restrict_origin
        self.logger = logging.getLogger(__name__)

        # We use a tuple (host, port) as key to ensure
        # that we do not forward to the same host twice
        self._targets = {}

        self._lock = threading.Lock()

    def connect(self, api):
        """
        Attach to a HA instance and forward events.

        Will overwrite old target if one exists with same host/port.
        """
        with self._lock:
            if len(self._targets) == 0:
                # First target we get, setup listener for events
                self.hass.bus.listen(ha.MATCH_ALL, self._event_listener)

            key = (api.host, api.port)

            self._targets[key] = api

    def disconnect(self, api):
        """ Removes target from being forwarded to. """
        with self._lock:
            key = (api.host, api.port)

            did_remove = self._targets.pop(key, None) is None

            if len(self._targets) == 0:
                # Remove event listener if no forwarding targets present
                self.hass.bus.remove_listener(ha.MATCH_ALL,
                                              self._event_listener)

            return did_remove

    def _event_listener(self, event):
        """ Listen and forwards all events. """
        with self._lock:
            # We don't forward time events or, if enabled, non-local events
            if event.event_type == ha.EVENT_TIME_CHANGED or \
               (self.restrict_origin and event.origin != self.restrict_origin):
                return

            for api in self._targets.values():
                fire_event(api, event.event_type, event.data, self.logger)


class StateMachine(ha.StateMachine):
    """
    Fires set events to an API.
    Uses state_change events to track states.
    """

    def __init__(self, bus, api):
        super().__init__(None)

        self.logger = logging.getLogger(__name__)

        self._api = api

        self.mirror()

        bus.listen(ha.EVENT_STATE_CHANGED, self._state_changed_listener)

    def set(self, entity_id, new_state, attributes=None):
        """ Calls set_state on remote API . """
        set_state(self._api, entity_id, new_state, attributes)

    def mirror(self):
        """ Discards current data and mirrors the remote state machine. """
        self._states = get_states(self._api, self.logger)

    def _state_changed_listener(self, event):
        """ Listens for state changed events and applies them. """
        self._states[event.data['entity_id']] = event.data['new_state']


class JSONEncoder(json.JSONEncoder):
    """ JSONEncoder that supports Home Assistant objects. """

    def default(self, obj):  # pylint: disable=method-hidden
        """ Checks if Home Assistat object and encodes if possible.
        Else hand it off to original method. """
        if isinstance(obj, ha.State):
            return obj.as_dict()

        return json.JSONEncoder.default(self, obj)


def validate_api(api):
    """ Makes a call to validate API. """
    try:
        req = api(METHOD_GET, URL_API)

        if req.status_code == 200:
            return APIStatus.OK

        elif req.status_code == 401:
            return APIStatus.INVALID_PASSWORD

        else:
            return APIStatus.UNKNOWN

    except ha.HomeAssistantError:
        return APIStatus.CANNOT_CONNECT


def connect_remote_events(from_api, to_api):
    """ Sets up from_api to forward all events to to_api. """

    data = {'host': to_api.host, 'api_password': to_api.api_password}

    if to_api.port is not None:
        data['port'] = to_api.port

    try:
        from_api(METHOD_POST, URL_API_EVENT_FORWARD, data)

    except ha.HomeAssistantError:
        pass


def disconnect_remote_events(from_api, to_api):
    """ Disconnects forwarding events from from_api to to_api. """
    data = {'host': to_api.host, '_METHOD': 'DELETE'}

    if to_api.port is not None:
        data['port'] = to_api.port

    try:
        from_api(METHOD_POST, URL_API_EVENT_FORWARD, data)

    except ha.HomeAssistantError:
        pass


def get_event_listeners(api, logger=None):
    """ List of events that is being listened for. """
    try:
        req = api(METHOD_GET, URL_API_EVENTS)

        return req.json()['event_listeners'] if req.status_code == 200 else {}

    except (ha.HomeAssistantError, ValueError, KeyError):
        # ValueError if req.json() can't parse the json
        # KeyError if 'event_listeners' not found in parsed json
        if logger:
            logger.exception("Bus:Got unexpected result")

        return {}


def fire_event(api, event_type, event_data=None, logger=None):
    """ Fire an event at remote API. """

    if event_data:
        data = {'event_data': json.dumps(event_data, cls=JSONEncoder)}
    else:
        data = None

    try:
        req = api(METHOD_POST, URL_API_EVENTS_EVENT.format(event_type), data)

        if req.status_code != 200 and logger:
            logger.error(
                "Error firing event: {} - {}".format(
                    req.status_code, req.text))

    except ha.HomeAssistantError:
        pass


def get_state(api, entity_id, logger=None):
    """ Queries given API for state of entity_id. """

    try:
        req = api(METHOD_GET,
                  URL_API_STATES_ENTITY.format(entity_id))

        # req.status_code == 422 if entity does not exist

        return ha.State.from_dict(req.json()) \
            if req.status_code == 200 else None

    except (ha.HomeAssistantError, ValueError):
        # ValueError if req.json() can't parse the json
        if logger:
            logger.exception("Error getting state")

        return None


def get_states(api, logger=None):
    """ Queries given API for all states. """

    try:
        req = api(METHOD_GET,
                  URL_API_STATES)

        json_result = req.json()
        states = {}

        for entity_id, state_dict in json_result.items():
            state = ha.State.from_dict(state_dict)

            if state:
                states[entity_id] = state

        return states

    except (ha.HomeAssistantError, ValueError, AttributeError):
        # ValueError if req.json() can't parse the json
        # AttributeError if parsed JSON was not a dict
        if logger:
            logger.exception("Error getting state")

        return {}


def set_state(api, entity_id, new_state, attributes=None, logger=None):
    """ Tells API to update state for entity_id. """

    attributes = attributes or {}

    data = {'new_state': new_state,
            'attributes': json.dumps(attributes)}

    try:
        req = api(METHOD_POST,
                  URL_API_STATES_ENTITY.format(entity_id),
                  data)

        if req.status_code != 201 and logger:
            logger.error(
                "Error changing state: {} - {}".format(
                    req.status_code, req.text))

    except ha.HomeAssistantError:
        if logger:
            logger.exception("Error setting state to server")


def is_state(api, entity_id, state, logger=None):
    """ Queries API to see if entity_id is specified state. """
    cur_state = get_state(api, entity_id, logger)

    return cur_state and cur_state.state == state


def get_services(api, logger=None):
    """ Returns a dict with per domain the available services at API. """
    try:
        req = api(METHOD_GET, URL_API_SERVICES)

        return req.json()['services'] if req.status_code == 200 else {}

    except (ha.HomeAssistantError, ValueError, KeyError):
        # ValueError if req.json() can't parse the json
        # KeyError if not all expected keys are in the returned JSON
        if logger:
            logger.exception("ServiceRegistry:Got unexpected result")

        return {}


def call_service(api, domain, service, service_data=None, logger=None):
    """ Calls a service at the remote API. """
    event_data = service_data or {}
    event_data[ha.ATTR_DOMAIN] = domain
    event_data[ha.ATTR_SERVICE] = service

    fire_event(api, ha.EVENT_CALL_SERVICE, event_data, logger)
