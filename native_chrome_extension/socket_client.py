from multiprocessing.connection import Client
from loguru import logger
from openadapt import config, sockets


def main():
    conn = sockets.create_client_connection(config.SOCKET_PORT)
    logger.info(f"connection accepted from {conn.last_accepted=}")
    try:
        # Start the event loop
        while True:
            # Receiving message from Server
            response = sockets.client_receive_message(config.SOCKET_PORT)

            logger.info(f"received {response=}")
    except KeyboardInterrupt:
        logger.info("Script terminated by user.")
        conn.close()


if __name__ == "__main__":
    main()
