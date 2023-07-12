from multiprocessing.connection import Client
from loguru import logger
import time
from openadapt import config

SERVER_SENDS = True
PORT = 6001
RETRY_INTERVAL = 5  # seconds

def establish_connection():
    address = ('localhost', config.SOCKET_PORT)
    conn = Client(address, authkey=b'openadapt')
    logger.info("Connected to the server.")
    return conn

def main():
    conn = establish_connection()

    while True:
        try:
            if SERVER_SENDS:
                logger.info("Waiting for message...")
                msg = conn.recv()
                logger.info(f"{msg=}")
            else:
                t = time.time()
                logger.info(f"Sending {t=}")
                conn.send(t)
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
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Script terminated by user.")
    except Exception as exc:
        logger.error(f"An unexpected error occurred: {exc}")