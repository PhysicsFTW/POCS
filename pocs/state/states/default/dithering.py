def on_enter(event_data):
    """Pointing State

    Take 30 second exposure and plate-solve to get the pointing error
    """
    pocs = event_data.model

    pocs.next_state = 'parking'

    try:
        pocs.say("It's dither time!")

        current_observation = pocs.observatory.current_observation

        pocs.logger.debug("Setting dithering coords: {}".format(current_observation.field))

        if pocs.observatory.mount.set_target_coordinates(current_observation.field):

            # TODO: Turn off autoguider

            pocs.observatory.mount.slew_to_target()

            # Wait until mount is_tracking, then transition to track state
            pocs.say("I'm moving to new dither position")

            while not pocs.observatory.mount.is_tracking:
                pocs.logger.debug("Slewing to target")
                pocs.sleep()

        pocs.next_state = 'observing'

    except Exception as e:
        pocs.logger.warning("Problem with preparing: {}".format())