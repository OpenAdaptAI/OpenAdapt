"""Lists all recordings in the database."""

from openadapt.db.crud import get_all_recordings


def main() -> None:
    """Prints all recordings in the database."""
    latest = " [latest]"
    for idx, recording in enumerate(get_all_recordings(), start=1):
        print(f"[{idx}]: {recording.task_description} | {recording.timestamp}" + latest)
        latest = ""


if __name__ == "__main__":
    main()
