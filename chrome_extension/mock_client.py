"""Mock client for testing the server."""
import time
import json

from loguru import logger

from openadapt import config, sockets

RETRY_INTERVAL = 5
SERVER_SENDS = False


def establish_connection() -> sockets.Connection:
    """Establish a connection to the server."""
    return sockets.create_client_connection(config.SOCKET_PORT)


def main() -> None:
    """Main function."""
    conn = establish_connection()
    while True:
        try:
            if SERVER_SENDS:
                if conn.closed:
                    conn = establish_connection()
                else:
                    logger.info("Waiting for message...")
                    msg = conn.recv()
                    logger.info(f"{msg=}")
            else:
                t = time.time()
                logger.info(f"Sending {t=}")
                conn.send(json.dumps(t))
                time.sleep(1)
        except EOFError:
            logger.warning("Connection closed. Reconnecting...")
            while True:
                try:
                    conn = establish_connection()
                    break
                except Exception as exc:
                    logger.warning(f"Failed to reconnect: {exc}")
                    time.sleep(RETRY_INTERVAL)
        except Exception as exc:
            logger.warning(f"Error during communication: {exc}")
            time.sleep(RETRY_INTERVAL)

    conn.close()


if __name__ == "__main__":
    main()
