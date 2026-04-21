import asyncio
import logging
import discord


def _is_missing_http_client(obj):
    return obj is discord.utils.MISSING or type(obj).__name__ == '_MissingSentinel'


async def ensure_http_client_ready(bot_instance):
    """Ensure the bot HTTP client is initialized and ready for API fetches."""
    if bot_instance is None:
        return None

    def set_http_client(target, client):
        try:
            target.http = client
        except Exception as exc:
            logging.debug(f"Could not set {type(target).__name__}.http directly: {exc}")
        try:
            target.__dict__['http'] = client
        except Exception:
            pass
        try:
            setattr(target, '_http', client)
        except Exception:
            pass

    def get_token():
        token = getattr(bot_instance, 'token', None)
        if token:
            return token
        if hasattr(bot_instance, '_connection'):
            token = getattr(bot_instance._connection, 'token', None) or getattr(bot_instance._connection, '_token', None)
            return token
        return None

    http_client = getattr(bot_instance, 'http', None)
    if _is_missing_http_client(http_client):
        http_client = None

    connection_http = None
    if hasattr(bot_instance, '_connection'):
        connection_http = getattr(bot_instance._connection, 'http', None)
        if _is_missing_http_client(connection_http):
            connection_http = None

    if http_client is None and connection_http is not None:
        http_client = connection_http

    if http_client is None:
        try:
            loop = asyncio.get_running_loop()
            http_client = discord.http.HTTPClient(loop)
            logging.info("✅ Created new bot HTTP client")
        except Exception as exc:
            logging.warning(f"Could not create bot HTTP client: {exc}")
            return None

    set_http_client(bot_instance, http_client)
    if hasattr(bot_instance, '_connection'):
        set_http_client(bot_instance._connection, http_client)

    token = get_token()
    session = getattr(http_client, '_HTTPClient__session', None)

    if session is discord.utils.MISSING or session is None or type(session).__name__ == '_MissingSentinel':
        if token:
            if getattr(http_client, 'token', None) is None:
                http_client.token = token
            try:
                logging.info("Initializing HTTP client session via static_login...")
                await http_client.static_login(token)
                logging.info("✅ HTTP client session initialized successfully")
            except Exception as exc:
                logging.warning(f"Could not initialize HTTP session: {exc}")
        else:
            logging.debug("HTTP client has no token available for session initialization")

    if type(getattr(http_client, '_global_over', None)).__name__ == '_MissingSentinel':
        try:
            http_client._global_over = asyncio.Event()
            http_client._global_over.set()
            logging.info("✅ Patched bot.http._global_over event for HTTP client")
        except Exception as exc:
            logging.debug(f"Could not patch _global_over: {exc}")

    if hasattr(bot_instance, '_connection'):
        logging.debug(
            "HTTP client state after repair: bot.http=%s bot._connection.http=%s",
            type(getattr(bot_instance, 'http', None)).__name__,
            type(getattr(bot_instance._connection, 'http', None)).__name__,
        )
    else:
        logging.debug("HTTP client state after repair: bot.http=%s", type(getattr(bot_instance, 'http', None)).__name__)

    return http_client
