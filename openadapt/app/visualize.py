"""Implements visualization utilities for OpenAdapt."""

from base64 import b64encode
from functools import partial
from os import path, sep
from pprint import pformat

from loguru import logger
from nicegui import events, ui

from openadapt.build_utils import redirect_stdout_stderr

with redirect_stdout_stderr():
    from tqdm import tqdm

import click

from openadapt.config import config
from openadapt.db.crud import get_latest_recording, get_recording
from openadapt.events import get_events
from openadapt.utils import (
    EMPTY,
    configure_logging,
    display_event,
    image2utf8,
    plot_performance,
    row2dict,
    rows2dicts,
)

SCRUB = config.SCRUB_ENABLED

if SCRUB:
    # too many warnings from scrubbing
    __import__("warnings").filterwarnings("ignore", category=DeprecationWarning)
    from openadapt.privacy.providers.presidio import PresidioScrubbingProvider

    scrub = PresidioScrubbingProvider()


LOG_LEVEL = "INFO"
MAX_EVENTS = None
PROCESS_EVENTS = True

performance_plot_img: ui.interactive_image = None


def create_tree(
    tree_dict: dict, max_children: str = config.VISUALIZE_MAX_TABLE_CHILDREN
) -> list[dict]:
    """Recursively creates a tree from a dictionary.

    Args:
        tree_dict (dict): The dictionary to create a tree from.
        max_children (str, optional): The maximum number of children to display.
        Defaults to MAX_TABLE_CHILDREN.

    Returns:
        list[dict]: Data for a Quasar Tree.
    """
    tree_data = []
    for key, value in tree_dict.items():
        if value in EMPTY:
            continue
        node = {
            "id": (
                str(key)
                + f"{(': '  + str(value)) if not isinstance(value, (dict, list)) else ''}"  # noqa
            )
        }
        if isinstance(value, dict):
            node["children"] = create_tree(value)
        elif isinstance(value, list):
            if max_children is not None and len(value) > max_children:
                node["children"] = create_tree(
                    {i: v for i, v in enumerate(value[:max_children])}
                )
                node["children"].append({"id": "..."})
            else:
                node["children"] = create_tree({i: v for i, v in enumerate(value)})
        tree_data.append(node)
    return tree_data


def set_tree_props(tree: ui.tree) -> None:
    """Sets properties for a UI tree based on values from config.

    Args:
      tree (ui.tree): A Quasar Tree.
    """
    tree._props["dense"] = config.VISUALIZE_DENSE_TREES
    tree._props["no-transition"] = not config.VISUALIZE_ANIMATIONS
    tree._props["default-expand-all"] = config.VISUALIZE_EXPAND_ALL
    tree._props["filter"] = ""


def set_filter(
    text: str,
    tree: ui.tree,
) -> None:
    """Sets filter for UI trees.

    Args:
        tree (ui.tree): A Quasar Tree.
        text (str): The text to filter by.
    """
    tree._props["filter"] = text
    tree.update()


def toggle_dark_mode(
    ui_dark: ui.dark_mode, plots: tuple[str], curr_logo: ui.image, images: tuple[str]
) -> None:
    """Handles dark mode toggle.

    Args:
        ui_dark (ui.dark_mode): The dark mode switch.
        plots (tuple[str]): The performance plots.
        curr_logo (ui.image): The current logo.
        images (tuple[str]): The light and dark mode logos (decoded)
    """
    global performance_plot_img
    ui_dark.toggle()
    ui_dark.update()
    config.VISUALIZE_DARK_MODE = ui_dark.value
    curr_logo.source = images[int(ui_dark.value)]
    curr_logo.update()
    performance_plot_img.source = plots[int(ui_dark.value)]
    performance_plot_img.update()


@logger.catch
@click.command()
@click.option(
    "--timestamp",
    type=str,
    help="The timestamp of the recording to visualize.",
)
def main(timestamp: str) -> None:
    """Visualize a recording."""
    configure_logging(logger, LOG_LEVEL)

    ui_dark = ui.dark_mode(config.VISUALIZE_DARK_MODE)

    if timestamp is None:
        recording = get_latest_recording()
    else:
        recording = get_recording(timestamp)

    if SCRUB:
        scrub.scrub_text(recording.task_description)
    logger.debug(f"{recording=}")

    meta = {}
    action_events = get_events(recording, process=PROCESS_EVENTS, meta=meta)
    event_dicts = rows2dicts(action_events)

    if SCRUB:
        event_dicts = scrub.scrub_list_dicts(event_dicts)
    logger.info(f"event_dicts=\n{pformat(event_dicts)}")

    recording_dict = row2dict(recording)

    if SCRUB:
        recording_dict = scrub.scrub_dict(recording_dict)

    # setup tables for recording / metadata
    recording_column = [
        (
            {
                "name": key,
                "field": key,
                "label": key,
                "sortable": False,
                "required": False,
                "align": "left",
            }
        )
        for key in recording_dict.keys()
    ]

    meta_col = [
        {
            "name": key,
            "field": key,
            "label": key,
            "sortable": False,
            "required": False,
            "align": "left",
        }
        for key in meta.keys()
    ]

    plots = (
        plot_performance(recording.timestamp, save_file=False, view_file=False),
        plot_performance(
            recording.timestamp, save_file=False, view_file=False, dark_mode=True
        ),
    )

    with ui.row():
        with ui.avatar(color="auto", size=128):
            images = ()

            # generate base64 encoded images for light and dark mode
            for i in range(2):
                fp = f"{path.dirname(__file__)}{sep}assets{sep}logo"
                logo_base64 = b64encode(
                    open(
                        f"{fp}{'_inverted' if i > 0 else ''}.png",
                        "rb",
                    ).read()
                )
                images += (
                    bytes(
                        f"data:image/png;base64,{(logo_base64.decode('utf-8'))}",
                        encoding="utf-8",
                    ).decode("utf-8"),
                )
            curr_logo = ui.image(images[int(ui_dark.value)])
        ui.switch(
            text="Dark Mode",
            value=ui_dark.value,
            on_change=partial(toggle_dark_mode, ui_dark, plots, curr_logo, images),
        )

    # create splitter with recording info on left and performance plot on right
    with ui.splitter(value=20).style("flex-wrap: nowrap;") as splitter:
        splitter._props["limits"] = [20, 80]

        # TODO: find a way to set "overflow: hidden;" for the splitter
        with splitter.before:
            ui.table(rows=[meta], columns=meta_col).style("min-width: 50em;")._props[
                "grid"
            ] = True
        with splitter.after:
            global performance_plot_img
            performance_plot_img = ui.interactive_image(
                source=plots[int(ui_dark.value)]
            )
            with performance_plot_img:
                # ui.button(
                #     on_click=lambda: plot_performance(
                #         recording.timestamp, view_file=True, save_file=False
                #     ),
                #     icon="visibility",
                # ).props("flat fab").tooltip("View")

                ui.button(
                    on_click=lambda: plot_performance(
                        recording.timestamp,
                        save_file=True,
                        view_file=False,
                        dark_mode=ui_dark.value,
                    ),
                    icon="save",
                ).props("flat fab").tooltip("Save as PNG")

    ui.table(rows=[recording_dict], columns=recording_column)

    interactive_images = []
    action_event_trees = []
    window_event_trees = []
    text_inputs = []

    logger.info(f"{len(action_events)=}")

    num_events = (
        min(MAX_EVENTS, len(action_events))
        if MAX_EVENTS is not None
        else len(action_events)
    )

    # global search
    def on_change_closure(e: events.ValueChangeEventArguments) -> None:
        for tree in range(len(action_event_trees)):
            set_filter(
                e.value,
                action_event_trees[tree],
            )
            set_filter(
                e.value,
                window_event_trees[tree],
            )

    text_inputs.append(
        ui.input(
            label="search all",
            placeholder="filter all",
            on_change=partial(on_change_closure),
        ).tooltip(
            "this will search all trees, but can be overridden by individual filters"
        )
    )

    with redirect_stdout_stderr():
        with tqdm(
            total=num_events,
            desc="Generating Visualization" if not SCRUB else "Scrubbing Visualization",
            unit="event",
            colour="green",
            dynamic_ncols=True,
        ) as progress:
            for idx, action_event in enumerate(action_events):
                if idx == MAX_EVENTS:
                    break

                image = display_event(action_event)
                # diff = display_event(action_event, diff=True)
                # mask = action_event.screenshot.diff_mask

                if SCRUB:
                    image = scrub.scrub_image(image)
                #    diff = scrub.scrub_image(diff)
                #    mask = scrub.scrub_image(mask)

                image_utf8 = image2utf8(image)
                # diff_utf8 = image2utf8(diff)
                # mask_utf8 = image2utf8(mask)
                width, height = image.size

                action_event_dict = row2dict(action_event)
                window_event_dict = row2dict(action_event.window_event)

                if SCRUB:
                    action_event_dict = scrub.scrub_dict(action_event_dict)
                    window_event_dict = scrub.scrub_dict(window_event_dict)

                with ui.column():
                    with ui.row():
                        interactive_images.append(
                            ui.interactive_image(
                                source=image_utf8,
                            ).classes("drop-shadow-md rounded")
                        )
                with ui.splitter(value=60) as splitter:
                    splitter.classes("w-full h-full")
                    with splitter.after:
                        ui.label("action_event_dict").style("font-weight: bold;")

                        def on_change_closure(
                            e: events.ValueChangeEventArguments, idx: int
                        ) -> None:
                            return set_filter(
                                e.value,
                                action_event_trees[idx],
                            )

                        text_inputs.append(
                            ui.input(
                                label="search",
                                placeholder="filter",
                                on_change=partial(
                                    on_change_closure,
                                    idx=idx,
                                ),
                            )
                        )
                        ui.html("<br/>")
                        action_event_tree = create_tree(action_event_dict)
                        action_event_trees.append(
                            ui.tree(
                                action_event_tree,
                                label_key="id",
                                # on_select=lambda e: ui.notify(e.value),
                            )
                        )
                        set_tree_props(action_event_trees[idx])
                    with splitter.before:
                        ui.label("window_event_dict").style("font-weight: bold;")

                        def on_change_closure(
                            e: events.ValueChangeEventArguments, idx: int
                        ) -> None:
                            return set_filter(
                                e.value,
                                window_event_trees[idx],
                            )

                        text_inputs.append(
                            ui.input(
                                label="search",
                                placeholder="filter",
                                on_change=partial(
                                    on_change_closure,
                                    idx=idx,
                                ),
                            )
                        )
                        ui.html("<br/>")
                        window_event_tree = create_tree(window_event_dict, None)

                        window_event_trees.append(
                            ui.tree(
                                window_event_tree,
                                label_key="id",
                                # on_select=lambda e: ui.notify(e.value),
                            )
                        )

                        set_tree_props(window_event_trees[idx])

                progress.update()

            progress.close()

    ui.run(
        reload=False,
        title=f"OpenAdapt: recording-{recording.id}",
        favicon="📊",
        native=config.VISUALIZE_RUN_NATIVELY,
        fullscreen=config.VISUALIZE_RUN_NATIVELY,
    )


if __name__ == "__main__":
    main()
