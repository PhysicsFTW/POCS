from ....utils import error
from time import sleep


def on_enter(event_data):
    """ """
    pocs = event_data.model
    pocs.say("I'm finding exoplanets!")
    pocs.next_state = 'parking'

    try:
        # Block on observing
        camera_events = pocs.observatory.observe()

        wait_time = 0.
        while not all([event.is_set() for event in camera_events.values()]):
            pocs.logger.debug('Still waiting for images: {} seconds'.format(wait_time))
            sleep(30.)
            wait_time += 30.

    except error.Timeout as e:
        pocs.logger.warning("Timeout while waiting for images. Something wrong with camera, going to park.")
    except Exception as e:
        pocs.logger.warning("Problem with imaging: {}".format(e))
        pocs.say("Hmm, I'm not sure what happened with that exposure.")
    else:
        pocs.next_state = 'analyzing'
