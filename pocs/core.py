import os
import queue
import signal
import time
import zmq

from multiprocessing import Process
from multiprocessing import Queue

from astropy import units as u

from . import PanBase
from .observatory import Observatory
from .state.machine import PanStateMachine
from .utils import current_time
from .utils import get_free_space
from .utils.messaging import PanMessaging


class POCS(PanStateMachine, PanBase):

    """The main class representing the Panoptes Observatory Control Software (POCS).
    Interaction with a PANOPTES unit is done through instances of this class. An instance consists
    primarily of an `Observatory` object, which contains the mount, cameras, scheduler, etc.
    See `pocs.Observatory`. The instance itself is designed to be run as a state machine with
    the `get_ready()` method the transition that is responsible for moving to the initial state.
    Args:
        state_machine_file(str): Filename of the state machine to use, defaults to 'simple_state_table'
        messaging(bool): If messaging should be included, defaults to False
        simulator(list): A list of the different modules that can run in simulator mode. Possible
            modules include: all, mount, camera, weather, night. Defaults to an empty list.
    Attributes:
        name (str): Name of PANOPTES unit
        next_state (str): The next state for the state machine
        observatory (`pocs.observatory.Observatory`): The `~pocs.observatory.Observatory` object
    """

    def __init__(self, state_machine_file=None, messaging=False, **kwargs):

        # Explicitly call the base classes in the order we want
        PanBase.__init__(self, **kwargs)

        self.logger.info('Initializing PANOPTES unit')
        self.logger.info("PANDIR: {}".format(os.environ['PANDIR']))
        self.logger.info("POCS: {}".format(os.environ['POCS']))

        self._processes = {}

        self._has_messaging = None
        self.has_messaging = messaging

        self._sleep_delay = kwargs.get('sleep_delay', 2.5)  # Loop delay
        self._safe_delay = kwargs.get('safe_delay', 60 * 5)  # Safety check delay
        self._is_safe = False

        if state_machine_file is None:
            state_machine_file = self.config['state_machine']

        PanStateMachine.__init__(self, state_machine_file, **kwargs)

        # Create our observatory, which does the bulk of the work
        self.observatory = Observatory(**kwargs)

        self.take_evening_flats = kwargs.get('take_evening_flats', False)

        self._connected = True
        self._initialized = False
        self._interrupted = False
        self.force_reschedule = False

        self.status()

        self.name = self.config.get('name', 'Generic PANOPTES Unit')
        self.logger.info('Welcome {}!'.format(self.name))
        self.say("Hi there!")

    @property
    def is_initialized(self):
        """ Indicates if POCS has been initalized or not """
        return self._initialized

    @property
    def interrupted(self):
        """If POCS has been interrupted
        Returns:
            bool: If an interrupt signal has been received
        """
        return self._interrupted

    @property
    def connected(self):
        """ Indicates if POCS is connected """
        return self._connected

    @property
    def has_messaging(self):
        return self._has_messaging

    @has_messaging.setter
    def has_messaging(self, value):
        self._has_messaging = value
        if self._has_messaging:
            self._setup_messaging()


##################################################################################################
# Methods
##################################################################################################

    def initialize(self):
        """ """

        if not self._initialized:
            self.say("Initializing the system! Woohoo!")

            try:
                # Initialize the mount
                self.logger.debug("Initializing mount")
                self.observatory.mount.initialize()

                if self.observatory.has_autoguider:
                    self.logger.debug("Connecting to autoguider")
                    self.observatory.autoguider.connect()

            except Exception as e:
                self.say("Oh wait. There was a problem initializing: {}".format(e))
                self.say("Since we didn't initialize, I'm going to exit.")
                self.power_down()
            else:
                self._initialized = True

        self.status()
        return self._initialized

    def status(self):
        status = dict()

        try:
            status['state'] = self.state
            status['system'] = {
                'free_space': get_free_space().value,
            }
            status['observatory'] = self.observatory.status()
        except Exception as e:  # pragma: no cover
            self.logger.warning("Can't get status: {}".format(e))
        else:
            self.send_message(status, channel='STATUS')

        return status

    def say(self, msg):
        """ PANOPTES Units like to talk!
        Send a message. Message sent out through zmq has unit name as channel.
        Args:
            msg(str): Message to be sent
        """
        self.send_message(msg, channel='PANCHAT')

    def send_message(self, msg, channel='POCS'):
        """ Send a message
        This will use the `self._msg_publisher` to send a message
        Note:
            The `channel` and `msg` params are switched for convenience
        Arguments:
            msg {str} -- Message to be sent
        Keyword Arguments:
            channel {str} -- Channel to send message on (default: {'POCS'})
        """
        if self.has_messaging:
            self._msg_publisher.send_message(channel, msg)

    def check_messages(self):
        """ Check messages for the system
        If `self.has_messaging` is True then there is a separate process runing
        responsible for checking incoming zeromq messages. That process will fill
        various `queue.Queue`s with messages depending on their type. This method
        is a thin-wrapper around private methods that are responsible for message
        dispatching based on which queue received a message.
        """
        if self.has_messaging:
            self._check_messages('command', self._cmd_queue)
            self._check_messages('schedule', self._sched_queue)

    def power_down(self, park=True):
        """Actions to be performed upon shutdown
        Note:
            This method is automatically called from the interrupt handler. The definition should
            include what you want to happen upon shutdown but you don't need to worry about calling
            it manually.
        """
        if self.connected:
            self.say("I'm powering down")
            self.logger.info("Shutting down {}, please be patient and allow for exit.".format(self.name))

            if park:
                # Park if needed
                if self.state not in ['parking', 'parked', 'sleeping', 'housekeeping']:
                    if self.observatory.mount.is_connected:
                        if not self.observatory.mount.is_parked:
                            self.logger.info("Parking mount")
                            self.park()

                if self.state == 'parking':
                    if self.observatory.mount.is_connected:
                        if self.observatory.mount.is_parked:
                            self.logger.info("Mount is parked, setting Parked state")
                            self.set_park()

                if not self.observatory.mount.is_parked:
                    self.logger.info('Mount not parked, parking')
                    self.observatory.mount.park()

            # Observatory shut down
            self.observatory.power_down()

            # Shut down messaging
            self.logger.debug('Shutting down messaging system')

            for name, proc in self._processes.items():
                if proc.is_alive():
                    self.logger.debug('Terminating {} - PID {}'.format(name, proc.pid))
                    proc.terminate()
                    os.remove('/tmp/POCS_{}.PID'.format(name.upper()))

            self._keep_running = False
            self._do_states = False
            self._connected = False
            self.logger.info("Power down complete")


##################################################################################################
# Safety Methods
##################################################################################################

    def is_safe(self):
        """Checks the safety flag of the system to determine if safe.
        This will check the weather station as well as various other environmental
        aspects of the system in order to determine if conditions are safe for operation.
        Note:
            This condition is called by the state machine during each transition
        Args:
            called from the state machine.
        Returns:
            bool: Latest safety flag
        """
        is_safe_values = dict()

        # Check if night time
        is_safe_values['is_dark'] = self.is_dark()

        # Check weather
        is_safe_values['good_weather'] = self.is_weather_safe()

        is_safe_values['free_space'] = self.has_free_space()

        safe = all(is_safe_values.values())

        if not safe:
            self.logger.warning('Unsafe conditions: {}'.format(is_safe_values))

            # Not safe so park unless we are not active
            if self.state not in ['sleeping', 'parked', 'parking', 'housekeeping', 'ready']:
                self.logger.warning('Safety failed so sending to park')
                self.park()

        return safe

    def is_dark(self):
        """Is it dark
        Checks whether it is dark at the location provided. This checks for the config
        entry `location.horizon` or 18 degrees (astronomical twilight).
        Returns:
            bool: Is night at location
        """
        if 'night' in self.config['simulator']:
            self.logger.debug("Night simulator says safe")
            is_dark = True
        else:
            is_dark = self.observatory.is_dark

        self.logger.debug("Dark Check: {}".format(is_dark))
        return is_dark

    def is_weather_safe(self, stale=180):
        """Determines whether current weather conditions are safe or not
        Args:
            stale (int, optional): Number of seconds before record is stale, defaults to 180
        Returns:
            bool: Conditions are safe (True) or unsafe (False)
        """
        assert self.db.current, self.logger.warning("No connection to sensors, can't check weather safety")

        # Always assume False
        is_safe = False
        record = {'safe': False}

        if 'weather' in self.config['simulator']:
            self.logger.debug("Weather simulator always safe")
            is_safe = True
        else:
            try:
                record = self.db.current.find_one({'type': 'weather'})

                is_safe = record['data'].get('safe', False)
                timestamp = record['date']
                age = (current_time().datetime - timestamp).total_seconds()

                self.logger.debug("Weather Safety: {} [{:.0f} sec old - {}]".format(is_safe, age, timestamp))

            except TypeError as e:
                self.logger.warning("No record found in Mongo DB")
                self.logger.debug('DB: {}'.format(self.db.current))
            else:
                if age > stale:
                    self.logger.warning("Weather record looks stale, marking unsafe.")
                    is_safe = False

        self._is_safe = is_safe

        return self._is_safe

    def has_free_space(self, required_space=0.25 * u.gigabyte):
        """Does hard drive have disk space (>= 0.5 GB)
        Args:
            required_space (u.gigabyte, optional): Amount of free space required
            for operation
        Returns:
            bool: True if enough space
        """
        free_space = get_free_space()
        return free_space.value >= required_space.to(u.gigabyte).value


##################################################################################################
# Convenience Methods
##################################################################################################

    def sleep(self, delay=2.5, with_status=True):
        """ Send POCS to sleep
        Loops for `delay` number of seconds. If `delay` is more than 10.0 seconds,
        `check_messages` will be called every 10.0 seconds in order to allow for
        interrupt.
        Keyword Arguments:
            delay {float} -- Number of seconds to sleep (default: 2.5)
            with_status {bool} -- Show system status while sleeping (default: {True if delay > 2.0})
        """
        if delay is None:
            delay = self._sleep_delay

        if with_status and delay > 2.0:
            self.status()

        # If delay is greater than 10 seconds check for messages during wait
        if delay >= 10.0:
            while delay >= 10.0:
                time.sleep(10.0)
                delay -= 10.0
                self.check_messages()

        if delay > 0.0:
            time.sleep(delay)

    def wait_until_safe(self):
        """ Waits until weather is safe
        This will wait until a True value is returned from the safety check,
        blocking until then.
        """
        while not self.is_safe():
            self.sleep(delay=self._safe_delay)


##################################################################################################
# Private Methods
##################################################################################################

    def _check_messages(self, queue_type, q):
        cmd_dispatch = {
            'command': {
                'park': self._interrupt_and_park,
                'shutdown': self._interrupt_and_shutdown,
            },
            'schedule': {
                'add': self._add_observation,
                'remove': self._remove_observation,
            }
        }

        while True:  # Will break if queue empty
            try:
                msg_obj = q.get_nowait()
                call_method = msg_obj.get('message', None)
                self.logger.info('Message received: {} {}'.format(queue_type, call_method))
                # Lookup and call the method
                cmd_dispatch[queue_type][call_method](msg_obj)
            except queue.Empty:
                break
            except KeyError:
                pass
            except Exception as e:
                self.logger.warning('Problem calling method from messaging: {}'.format(e))
            else:
                break

    def _interrupt_and_park(self, *arg):
        self.logger.info('Park interrupt received')
        self._interrupted = True
        self.park()

    def _interrupt_and_shutdown(self, *arg):
        self.logger.warning('Shutdown command received')
        self._interrupted = True
        self.power_down()

    def _add_observation(self, observations):

        for target in observations['targets']:
            self.logger.info('Recieving new target: ' + target['name'])
            self.observatory.scheduler.add_observation(target)
            self._interrupted = True

            try:
                if target.get('priority', 0.) >= self.observatory.current_observation.priority:
                    self.logger.info('Forcing a reschedule after next image')
                    self.force_reschedule = True
            except Exception as e:
                pass

    def _remove_observation(self, observations):

        for target in observations['targets']:
            try:
                self.logger.info('Removing target: ' + target['name'])
                self.observatory.scheduler.remove_observation(target)
                self._interrupted = True
            except Exception as e:
                pass

    def _setup_messaging(self):

        self._processes = {
            'check_messages': None,
            'cmd_forwarder': None,
            'msg_forwarder': None,
        }

        # Check PID for each type of process and kill if existing
        for name, proc in self._processes.items():
            pid_file_name = '/tmp/POCS_{}.PID'.format(name.upper())
            if os.path.exists(pid_file_name):
                pid = None
                with open(pid_file_name, 'r') as f:
                    pid = int(f.read())
                self.logger.warning("Killing stale messaging process: {} PID {}".format(name, pid))
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                finally:
                    os.remove(pid_file_name)

        cmd_port = self.config['messaging']['cmd_port']
        msg_port = self.config['messaging']['msg_port']

        def create_forwarder(port):
            PanMessaging.create_forwarder(port, port + 1)

        cmd_forwarder_process = Process(target=create_forwarder, args=(cmd_port,), name='CmdForwarder')
        cmd_forwarder_process.start()
        self._processes['cmd_forwarder'] = cmd_forwarder_process

        msg_forwarder_process = Process(target=create_forwarder, args=(msg_port,), name='MsgForwarder')
        msg_forwarder_process.start()
        self._processes['msg_forwarder'] = msg_forwarder_process

        self._do_cmd_check = True
        self._cmd_queue = Queue()
        self._sched_queue = Queue()

        self._msg_publisher = PanMessaging.create_publisher(msg_port)

        def check_message_loop(cmd_queue, sched_queue):
            cmd_subscriber = PanMessaging.create_subscriber(cmd_port + 1)

            poller = zmq.Poller()
            poller.register(cmd_subscriber.socket, zmq.POLLIN)

            try:
                while self._do_cmd_check:
                    # Poll for messages
                    sockets = dict(poller.poll(500))  # 500 ms timeout

                    if cmd_subscriber.socket in sockets and sockets[cmd_subscriber.socket] == zmq.POLLIN:

                        msg_type, msg_obj = cmd_subscriber.receive_message(flags=zmq.NOBLOCK)

                        # Put the message in a queue to be processed
                        if msg_type == 'POCS-CMD':
                            cmd_queue.put(msg_obj)
                        elif msg_type == 'POCS-SCHEDULE':
                            sched_queue.put(msg_obj)

                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        self.logger.debug('Starting command message loop')
        check_messages_process = Process(target=check_message_loop, args=(self._cmd_queue, self._sched_queue))
        check_messages_process.name = 'MessageCheckLoop'
        check_messages_process.start()
        self._processes['check_messages'] = check_messages_process
        self.logger.debug('Command message subscriber set up on port {}'.format(cmd_port))

        # Make PID file for each process
        for name, proc in self._processes.items():
            self.logger.debug("Writing PID file for {} {}".format(name, proc.pid))
            with open('/tmp/POCS_{}.PID'.format(name.upper()), 'w') as f:
                f.write("{}".format(proc.pid))
