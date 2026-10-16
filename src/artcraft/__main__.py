import logging

from dotenv import load_dotenv

from .bot import ArtCraftBot
from .config import Config


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = Config.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    bot = ArtCraftBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
