from __future__ import annotations

import discord


def is_mod(member: discord.Member | None, mod_role_id: int | None) -> bool:
    if member is None:
        return False
    if member.guild_permissions.administrator or member.guild_permissions.moderate_members:
        return True
    if mod_role_id and any(role.id == mod_role_id for role in member.roles):
        return True
    return False


def display_name(member: discord.abc.User | discord.Member) -> str:
    if isinstance(member, discord.Member):
        return member.display_name
    return member.name
