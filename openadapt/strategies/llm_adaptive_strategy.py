import time
import json
from pprint import pformat

from pynput import keyboard, mouse
import numpy as np
from PIL import ImageChops

from openadapt import adapters, models, playback, utils
from openadapt.strategies.base import BaseReplayStrategy, prompt_is_action_complete
from openadapt.custom_logger import logger
from openadapt.models import Recording, ActionEvent, Screenshot, WindowEvent


class LLMAdaptiveStrategy(BaseReplayStrategy):
    """
    An adaptive replay strategy powered by Large Language Models.
    This strategy aims to understand the intent behind recorded actions
    and adapt the replay to variations in UI or application state.
    """

    def __init__(
        self,
        recording: Recording,
        max_recorded_actions_to_consider: int = 10,
        ui_consistency_threshold: float = 0.05,
        force_llm_every_n_steps: int = 0,
        enable_error_recovery: bool = True,
        **kwargs,
    ) -> None:
        """
        Initializes the LLMAdaptiveStrategy.

        Args:
            recording: The recording object to replay.
            max_recorded_actions_to_consider: Max number of past recorded actions
                                              to include in the LLM prompt.
            ui_consistency_threshold: Max allowed difference percentage between screenshots
                                      for considering UI consistent.
            force_llm_every_n_steps: Call LLM every N steps regardless of UI consistency.
                                     0 means never force.
            enable_error_recovery: Whether to enable basic error detection after an action.
            **kwargs: Additional keyword arguments for the strategy.
        """
        super().__init__(recording, **kwargs)
        logger.info(f"Initialized LLMAdaptiveStrategy for recording: {recording.timestamp}")
        self.llm_adapter = adapters.get_default_prompt_adapter()
        self.processed_action_events = self.recording.processed_action_events
        # self.replayed_action_events = [] # Removed as per refactor
        self.current_original_action_idx = 0
        self.max_recorded_actions_to_consider = max_recorded_actions_to_consider
        self.ui_consistency_threshold = ui_consistency_threshold
        self.force_llm_every_n_steps = force_llm_every_n_steps
        self.steps_since_last_llm_call = 0
        self.enable_error_recovery = enable_error_recovery

        logger.info(f"Original recording has {len(self.processed_action_events)} actions.")
        logger.info(f"UI consistency threshold: {self.ui_consistency_threshold}")
        logger.info(f"Force LLM every N steps: {self.force_llm_every_n_steps}")
        logger.info(f"Error recovery enabled: {self.enable_error_recovery}")

    def _is_ui_consistent_for_next_original_action(
        self,
        current_screenshot: Screenshot,
        current_window_event: WindowEvent,
    ) -> bool:
        logger.debug("Checking UI consistency for next original action...")

        if self.current_original_action_idx >= len(self.processed_action_events):
            logger.debug("No more original actions to check against. UI considered inconsistent.")
            return False

        original_action = self.processed_action_events[self.current_original_action_idx]
        original_screenshot = original_action.screenshot
        original_window_event = original_action.window_event

        if not original_screenshot or not original_screenshot.image:
            logger.warning("Original action's screenshot or image is missing. UI considered inconsistent.")
            return False
        if not current_screenshot or not current_screenshot.image:
            logger.warning("Current screenshot or image is missing. UI considered inconsistent.")
            return False
        if not original_window_event:
            logger.warning("Original action's window event is missing. UI considered inconsistent.")
            return False
        if not current_window_event:
            logger.warning("Current window event is missing. UI considered inconsistent.")
            return False

        if current_window_event.title != original_window_event.title:
            logger.info(
                f"Window titles differ. Current: '{current_window_event.title}', "
                f"Original: '{original_window_event.title}'. UI inconsistent."
            )
            return False

        width_diff = abs(current_window_event.width - original_window_event.width)
        height_diff = abs(current_window_event.height - original_window_event.height)
        if original_window_event.width > 0 and \
           width_diff / original_window_event.width > 0.10:
            logger.info(
                f"Window widths differ significantly (>10%). Current: {current_window_event.width}, "
                f"Original: {original_window_event.width}. UI inconsistent."
            )
            return False
        if original_window_event.height > 0 and \
           height_diff / original_window_event.height > 0.10:
            logger.info(
                f"Window heights differ significantly (>10%). Current: {current_window_event.height}, "
                f"Original: {original_window_event.height}. UI inconsistent."
            )
            return False

        current_img = current_screenshot.image.convert('RGB')
        original_img = original_screenshot.image.convert('RGB')

        if current_img is original_img:
            logger.debug("Images are identical objects. UI consistent.")
            return True

        if current_img.size != original_img.size:
            logger.info(
                f"Image sizes differ. Current: {current_img.size}, Original: {original_img.size}. "
                "UI inconsistent."
            )
            return False

        try:
            diff_image = ImageChops.difference(current_img, original_img)
            diff_array = np.array(diff_image)
            if diff_array.size == 0:
                diff_percentage = 0.0
            else:
                diff_percentage = np.count_nonzero(diff_array) / diff_image.size[0] / diff_image.size[1] / 3

            logger.debug(f"Image difference percentage: {diff_percentage:.4f}")
            if diff_percentage > self.ui_consistency_threshold:
                logger.info(
                    f"Image difference ({diff_percentage:.4f}) exceeds threshold "
                    f"({self.ui_consistency_threshold}). UI inconsistent."
                )
                return False
        except Exception as e:
            logger.error(f"Error during image comparison: {e}. Assuming UI inconsistent.")
            return False

        logger.info("All UI consistency checks passed.")
        return True

    def get_next_action_event(
        self,
        screenshot: Screenshot,
        window_event: WindowEvent,
    ) -> ActionEvent | None:
        logger.debug(
            f"Getting next action event. Screenshot ts: {screenshot.timestamp}, "
            f"Window: {window_event.title}, "
            f"Steps since last LLM: {self.steps_since_last_llm_call}, "
            f"Total actions played so far: {len(self.action_events)}"
        )
        self.steps_since_last_llm_call += 1

        # Check if we have exhausted original actions.
        # The LLM might still want to do something (e.g., a final verification or a "stop" action).
        if self.current_original_action_idx >= len(self.processed_action_events):
            logger.info(
                "Reached end of original recorded actions. "
                "LLM will be consulted for any final actions or to stop."
            )
            # Fall through to LLM, it should decide if it's time to StopIteration

        use_llm = True
        if self.force_llm_every_n_steps > 0 and \
           self.steps_since_last_llm_call >= self.force_llm_every_n_steps:
            logger.info(
                f"Forcing LLM call because steps_since_last_llm_call "
                f"({self.steps_since_last_llm_call}) >= force_llm_every_n_steps "
                f"({self.force_llm_every_n_steps})."
            )
        elif self.current_original_action_idx < len(self.processed_action_events):
            # Only check for consistency if there are original actions left to compare against
            should_try_original = self._is_ui_consistent_for_next_original_action(
                screenshot, window_event
            )
            if should_try_original:
                use_llm = False
        # If no original actions left, use_llm remains True to consult LLM

        if not use_llm:
            # This block is only entered if should_try_original was True
            action_to_play = self.processed_action_events[self.current_original_action_idx]
            logger.info(
                f"UI is consistent. Replaying original action event at index "
                f"{self.current_original_action_idx}: {action_to_play.name}"
            )
            self.current_original_action_idx += 1
            # self.action_events.append(action_to_play) # Appended in run() method
            return action_to_play

        # If use_llm is True (forced, UI inconsistent, or end of original actions)
        self.steps_since_last_llm_call = 0
        logger.info("Using LLM to determine next action.")

        start_idx_orig = max(0, self.current_original_action_idx - self.max_recorded_actions_to_consider)
        end_idx_orig = min(len(self.processed_action_events), self.current_original_action_idx + 5)
        recorded_actions_segment = self.processed_action_events[start_idx_orig:end_idx_orig]

        recorded_actions_prompt_list = utils.rows2dicts(
            recorded_actions_segment,
            drop_empty=True, drop_constant=False, follow=False,
            custom_converters={ActionEvent: lambda ae: ae.to_prompt_dict()}
        )

        # Use self.action_events for replayed_actions context
        start_index_replayed = max(0, len(self.action_events) - self.max_recorded_actions_to_consider)
        actions_for_prompt = self.action_events[start_index_replayed:]
        replayed_actions_prompt_list = utils.rows2dicts(
            actions_for_prompt,
            drop_empty=True, drop_constant=False, follow=False,
            custom_converters={ActionEvent: lambda ae: ae.to_prompt_dict()}
        )

        replay_instructions = getattr(self.recording, 'task_description', '') or \
                              "Replay the sequence of actions. Adapt to minor UI changes if necessary."
        current_window_dict = window_event.to_prompt_dict(include_data=True)

        prompt_context = {
            "recorded_actions": json.dumps(recorded_actions_prompt_list, indent=2),
            "replayed_actions": json.dumps(replayed_actions_prompt_list, indent=2),
            "replay_instructions": replay_instructions,
            "current_window": json.dumps(current_window_dict, indent=2),
        }

        system_prompt = utils.render_template_from_file("prompts/system.j2")
        user_prompt = utils.render_template_from_file(
            "prompts/generate_action_event.j2", **prompt_context
        )

        logger.debug(f"System Prompt (len: {len(system_prompt)}): {system_prompt[:500]}...")
        logger.debug(f"User Prompt (len: {len(user_prompt)}): {user_prompt[:500]}...")

        try:
            img_for_prompt = screenshot.image
            if not img_for_prompt:
                logger.error("Screenshot image is None, cannot proceed with LLM prompt requiring image.")
                # Fallback or stop if no image? For now, let LLM decide without image if adapter handles it.
                # Or, raise StopIteration more definitively.
                raise StopIteration("Screenshot image is missing for LLM prompt.")

            llm_response_raw = self.llm_adapter.prompt(
                user_prompt, system_prompt=system_prompt, images=[img_for_prompt],
            )
            logger.info(f"LLM raw response: {llm_response_raw}")

            if not llm_response_raw or llm_response_raw.strip() == "{}": # Check for empty or "{}"
                # If we are past original actions, and LLM gives empty, it's time to stop.
                if self.current_original_action_idx >= len(self.processed_action_events):
                    logger.info("LLM returned empty response and no more original actions. Stopping.")
                    raise StopIteration("LLM returned empty response at end of original actions.")
                # Otherwise, this might be an LLM error or it deciding to do nothing for one step.
                # For now, treat as stop, could be refined to allow "no-op" from LLM
                logger.warning("LLM returned empty or trivial JSON object. Interpreting as stop.")
                raise StopIteration("LLM returned empty or trivial response.")


            action_dict = utils.parse_code_snippet(llm_response_raw)
            if not action_dict or not isinstance(action_dict, dict):
                logger.warning(f"Could not parse a valid dict from LLM response: {llm_response_raw}")
                raise StopIteration("Failed to parse LLM response into a dictionary.")

            if action_dict.get("name", "").lower() == "stop_replay":
                logger.info("LLM signaled to stop replay.")
                raise StopIteration("LLM decided to stop replay.")

            # If LLM returns an empty action or indicates no action needed,
            # and we are past original actions, stop.
            if not action_dict.get("name") and self.current_original_action_idx >= len(self.processed_action_events):
                 logger.info("LLM returned no specific action and no more original actions. Stopping.")
                 raise StopIteration("LLM returned no action at end of original actions.")


            if not action_dict.get("name"): # If still no name after specific checks, it's an issue.
                logger.warning(f"LLM response does not look like a valid action (missing 'name'): {action_dict}")
                raise StopIteration("LLM response not a valid action (missing 'name' field).")

            next_action_event = ActionEvent.from_dict(action_dict)
            next_action_event.screenshot_timestamp = screenshot.timestamp
            next_action_event.screenshot_id = screenshot.id
            next_action_event.window_event_timestamp = window_event.timestamp
            next_action_event.window_event_id = window_event.id

            # self.action_events.append(next_action_event) # Appended in run() method

            if self.current_original_action_idx < len(self.processed_action_events):
                 self.current_original_action_idx += 1

            logger.info(f"Generated next action via LLM: {next_action_event.name}")
            return next_action_event

        except StopIteration as si:
            logger.info(f"Stopping iteration: {si}")
            raise
        except Exception as e:
            logger.exception(f"Error during LLM interaction or action generation: {e}")
            raise StopIteration(f"Exception in LLM part of get_next_action_event: {e}")

    def run(self) -> None:
        """Run the replay strategy.

        This method is copied from BaseReplayStrategy and modified for LLMAdaptiveStrategy.
        """
        logger.info(f"Running {self!r}")

        self.mouse_controller = mouse.Controller()
        self.keyboard_controller = keyboard.Controller()

        event_states = {}  # Used to track pressed keys/buttons

        try:
            while True:
                screenshot = models.Screenshot.take_screenshot()
                window_event = models.WindowEvent.get_active_window_event()
                # Note: self.screenshots and self.window_events are from BaseReplayStrategy
                self.screenshots.append(screenshot)
                self.window_events.append(window_event)

                try:
                    action_event = self.get_next_action_event(
                        screenshot, window_event
                    )
                except StopIteration:
                    logger.info("StopIteration received, replay ending.")
                    break

                if not action_event: # Should be handled by StopIteration, but as safeguard
                    logger.info("get_next_action_event returned None, replay ending.")
                    break

                # Append action to self.action_events here, as it's confirmed to be played
                self.action_events.append(action_event)

                action_event_dict = action_event.to_dict(
                    exclude_unset=True,
                    exclude_none=True,
                    exclude_defaults=True, # TODO: confirm correct usage
                )
                action_event_dict.pop("screenshot", None) # too verbose
                logger.debug(f"action_event=\n{pformat(action_event_dict)}")

                # Check timestamp ordering
                if len(self.action_events) > 1:
                    prev_action_event = self.action_events[-2]
                    # Note: LLM-generated actions might not have increasing timestamps
                    # if their timestamps are derived from current time.
                    # Original actions replayed will have original timestamps.
                    # This assertion might need adjustment depending on how timestamps
                    # are assigned to LLM-generated actions.
                    # For now, we assume action_event.timestamp is the current play time.
                    # If action_event.timestamp is from original recording, this check is fine.
                    # If action_event is new, its timestamp will be current time.
                    # Let's assume action_event.timestamp is set appropriately by get_next_action_event
                    # or by playback.play_action_event if it modifies it.
                    # For now, this check is more about the list integrity.
                    current_ts = action_event.timestamp
                    prev_ts = prev_action_event.timestamp
                    if current_ts is None: # Should not happen if action is played
                        logger.warning("Current action event timestamp is None.")
                    if prev_ts is None: # Should not happen for prior actions
                        logger.warning("Previous action event timestamp is None.")

                    # This assertion is problematic if mixing original (past ts) and new (current ts)
                    # For LLM strategy, we might generate actions "now" for an "original" context.
                    # Let's comment it out for now, or make it more nuanced.
                    # if current_ts and prev_ts and current_ts < prev_ts:
                    #    logger.warning(
                    #        f"Timestamps are not monotonically increasing: "
                    #        f"{prev_action_event.timestamp=}, {action_event.timestamp=}"
                    #    )


                try:
                    playback.play_action_event(
                        action_event,
                        self.mouse_controller,
                        self.keyboard_controller,
                        event_states,
                    )
                except Exception as exc:
                    logger.exception(exc)
                    # TODO: LLM-based error recovery if self.enable_error_recovery
                    # For now, just log and continue or break
                    if self.enable_error_recovery:
                        logger.warning("Error during action playback. LLM will assess in next cycle.")
                        # Potentially pass this error info to the next LLM prompt
                    else:
                        logger.error("Error during action playback and recovery is disabled. Stopping.")
                        break # Or re-raise

                if self.enable_error_recovery and action_event:
                    logger.info(f"Checking outcome of action: {action_event.id if action_event.id else 'NEW'}")
                    # It might be better to take screenshot *before* next get_next_action_event call,
                    # but for now, this is post-action. The next loop iteration will take a fresh one.
                    post_action_screenshot = models.Screenshot.take_screenshot()

                    action_actually_completed = prompt_is_action_complete(
                        post_action_screenshot, # Current state of UI
                        self.action_events # History of actions taken
                    )
                    action_event_details = action_event.to_prompt_dict() if action_event else {}
                    if not action_actually_completed:
                        logger.warning(
                            f"Action (id={action_event.id if action_event.id else 'NEW'}, "
                            f"type={action_event.name if action_event else 'N/A'}, "
                            f"details={action_event_details}) "
                            f"may not have completed as expected according to LLM check. "
                            f"LLM will assess the new state in the next cycle."
                        )
                        # This information (that action may not have completed)
                        # should ideally be part of the context for the *next*
                        # call to get_next_action_event.
                    else:
                        logger.info(
                            f"Action (id={action_event.id if action_event.id else 'NEW'}, "
                            f"type={action_event.name if action_event else 'N/A'}) "
                            f"appears to have completed as expected according to LLM check."
                        )

                # TODO: make configurable
                time.sleep(self.config.replay_speed_multiplier * action_event.mouse_delay)


        except KeyboardInterrupt:
            logger.info("Replay interrupted by user (KeyboardInterrupt).")
        finally:
            playback.release_pressed_keys(self.keyboard_controller, event_states)
            playback.release_pressed_mouse_buttons(self.mouse_controller, event_states)
            logger.info(f"Finished {self!r}")

            # TODO: save self.action_events, self.screenshots, self.window_events
            # This is typically handled by the caller of strategy.run() in `replay.py`
            # by creating a new recording if `record_replay` is True.
            # For LLMAdaptiveStrategy, self.action_events now contains the actual sequence
            # of played actions (mix of original and LLM-generated).
            # The base class `finalize_replay` method might need to be aware of this
            # if it tries to save these directly.
            # For now, let's assume the standard replay flow handles saving.
            pass
