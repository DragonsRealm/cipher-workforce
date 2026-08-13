"""Discord plugin: bot messaging, REST actions and inbound events.

Pure wiring. Importing the node modules is what registers them --
``BaseNode.__init_subclass__`` does that work.

``import discord`` never appears at module scope anywhere in this package.
``nodes/__init__.py`` swallows import errors during discovery, so a library
import that failed here would make the whole plugin silently disappear rather
than report anything. The gateway imports it inside a function.
"""

from __future__ import annotations

from services.node_output_schemas import register_output_schema
from services.plugin.social_provider_registry import register_social_send_handler
from services.ws_handler_registry import register_option_loader

from ._credentials import DiscordBotCredential
from ._option_loaders import load_accounts, load_channels, load_guilds
from ._social import social_send_adapter
from .discord_action import DiscordActionNode, DiscordActionOutput
from .discord_send import DiscordSendNode, DiscordSendOutput

register_option_loader("discordAccounts", load_accounts)
register_option_loader("discordGuilds", load_guilds)
register_option_loader("discordChannels", load_channels)

register_output_schema(DiscordSendNode.type, DiscordSendOutput)
register_output_schema(DiscordActionNode.type, DiscordActionOutput)

# socialSend routes by platform id.
register_social_send_handler("discord", social_send_adapter)

__all__ = [
    "DiscordActionNode",
    "DiscordBotCredential",
    "DiscordSendNode",
]
