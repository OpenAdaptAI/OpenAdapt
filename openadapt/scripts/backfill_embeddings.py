"""This script backfills embeddings for existing recordings."""

import argparse

from sqlalchemy.orm import Session

from openadapt.db import crud
from openadapt.models import Recording, RecordingEmbedding
from openadapt.custom_logger import logger
from openadapt.embed import get_configured_model_name # Updated import

def backfill_embeddings(session: Session, force_update: bool = False) -> None:
    """Backfills embeddings for recordings that don't have one or if force_update is True.

    Args:
        session: The database session.
        force_update: If True, will re-generate embeddings even if they exist.
    """
    logger.info("Starting embedding backfill process...")
    recordings = session.query(Recording).all()
    logger.info(f"Found {len(recordings)} recordings to process.")

    processed_count = 0
    skipped_count = 0
    error_count = 0

    for recording in recordings:
        if not recording.task_description:
            # logger.info(f"Skipping recording_id={recording.id}, no task_description.")
            skipped_count += 1
            continue

        if not force_update:
            # Check if an embedding with the current MODEL_NAME already exists
            current_model_name = get_configured_model_name()
            existing_embedding = (
                session.query(RecordingEmbedding)
                .filter_by(recording_id=recording.id, model_name=current_model_name)
                .first()
            )
            if existing_embedding:
                # logger.info(
                #    f"Skipping recording_id={recording.id}, embedding with model='{current_model_name}' already exists."
                # )
                skipped_count += 1
                continue
        
        logger.info(f"Processing recording_id={recording.id}: '{recording.task_description[:50]}...' ")
        try:
            # add_or_update_embedding will handle both creation and update logic
            # It uses the MODEL_NAME from openadapt.embed
            crud.add_or_update_embedding(session, recording, recording.task_description)
            processed_count += 1
        except Exception as e:
            logger.error(f"Error processing recording_id={recording.id}: {e}")
            error_count += 1
            # Optionally, rollback session for this specific error to not affect others
            # session.rollback() 

    if processed_count > 0 or error_count > 0 : # only commit if there were changes or errors to log
        try:
            session.commit()
            logger.info("Committed changes to the database.")
        except Exception as e:
            logger.error(f"Error committing changes to database: {e}")
            session.rollback()
            
    logger.info(f"Embedding backfill complete. Processed: {processed_count}, Skipped: {skipped_count}, Errors: {error_count}")

def main() -> None:
    """Main function to run the backfill script."""
    parser = argparse.ArgumentParser(description="Backfill embeddings for existing recordings.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force update embeddings even if they already exist for the current model.",
    )
    args = parser.parse_args()
    db_session = None
    try:
        # Use read_and_write=True for the session
        db_session = crud.get_new_session(read_and_write=True)
        backfill_embeddings(db_session, force_update=args.force)
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        if db_session:
            db_session.rollback()
    finally:
        if db_session:
            db_session.close()
        # crud.release_db_lock()
        logger.info("Backfill script finished.")

if __name__ == "__main__":
    main() 