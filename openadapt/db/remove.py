"""Removes a recording from the database."""

import sys

from openadapt.db.crud import delete_recording, get_all_recordings


def main() -> int:
    """Removes a recording from the database."""
    recordings = get_all_recordings()

    if not recordings:
        print("No recordings found.")
        return 1

    if len(sys.argv) != 2:
        print("Usage: openadapt.db.remove <recording_index|latest|all>")
        print("Run `openadapt.db.list` to see all recordings.")
        return 1

    if sys.argv[1].casefold() == "all".casefold():
        if (
            input("Are you sure you want to delete all recordings? (y/n) ").casefold()
            == "y".casefold()
        ):
            for recording in recordings:
                delete_recording(recording.timestamp)
                print("All recordings deleted.")
        else:
            print("Aborting.")
            return 0
    elif sys.argv[1].casefold() == "latest".casefold():
        if (
            input(
                f"Are you sure you want to delete the latest recording"
                f" {recordings[0].task_description} | {recordings[0].timestamp}? (y/n) "
            ).casefold()
            == "y".casefold()
        ):
            delete_recording(recordings[0].timestamp)
            print("Latest recording deleted.")
        else:
            print("Aborting.")
    else:
        if not sys.argv[1].isdigit():
            print("Recording index must be an integer.")
            return 1

        if int(sys.argv[1]) > len(recordings):
            print("Recording index out of range.")
            return 1

        rec = recordings[int(sys.argv[1]) - 1]
        if (
            input(
                f"Are you sure you want to delete recording {rec.task_description}"
                f" | {rec.timestamp}? (y/n) "
            ).casefold()
            == "y".casefold()
        ):
            delete_recording(rec.timestamp)
            print("Recording deleted.")
        else:
            print("Aborting.")
    return 0


if __name__ == "__main__":
    main()
