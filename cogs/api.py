from discord.ext import commands
from .utils import fuzzy
import asyncio
import discord
import re
import zlib
import io
import os
import lxml.etree as etree

DISCORD_PY_GUILD_ID = 336642139381301249
ROBODANNY_ID = 80528701850124288

class SphinxObjectFileReader:
    # Inspired by Sphinx's InventoryFileReader
    BUFSIZE = 16 * 1024

    def __init__(self, buffer):
        self.stream = io.BytesIO(buffer)

    def readline(self):
        return self.stream.readline().decode('utf-8')

    def skipline(self):
        self.stream.readline()

    def read_compressed_chunks(self):
        decompressor = zlib.decompressobj()
        while True:
            chunk = self.stream.read(self.BUFSIZE)
            if len(chunk) == 0:
                break
            yield decompressor.decompress(chunk)
        yield decompressor.flush()

    def read_compressed_lines(self):
        buf = b''
        for chunk in self.read_compressed_chunks():
            buf += chunk
            pos = buf.find(b'\n')
            while pos != -1:
                yield buf[:pos].decode('utf-8')
                buf = buf[pos + 1:]
                pos = buf.find(b'\n')


class API(commands.Cog):
    """Discord API exclusive things."""

    def __init__(self, bot):
        self.bot = bot
        self.issue = re.compile(r'##(?P<number>[0-9]+)')
        self._recently_blocked = set()

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.guild.id != DISCORD_PY_GUILD_ID:
            return

        # this bot is only a backup for when R. Danny is offline
        robodanny = message.guild.get_member(ROBODANNY_ID)
        if robodanny and robodanny.status is not discord.Status.offline:
            return

        m = self.issue.search(message.content)
        if m is not None:
            url = 'https://github.com/Rapptz/discord.py/issues/'
            await message.channel.send(url + m.group('number'))

    def parse_object_inv(self, stream, url):
        # key: URL
        # n.b.: key doesn't have `discord` or `discord.ext.commands` namespaces
        result = {}

        # first line is version info
        inv_version = stream.readline().rstrip()

        if inv_version != '# Sphinx inventory version 2':
            raise RuntimeError('Invalid objects.inv file version.')

        # next line is "# Project: <name>"
        # then after that is "# Version: <version>"
        projname = stream.readline().rstrip()[11:]
        version = stream.readline().rstrip()[11:]

        # next line says if it's a zlib header
        line = stream.readline()
        if 'zlib' not in line:
            raise RuntimeError('Invalid objects.inv file, not z-lib compatible.')

        # This code mostly comes from the Sphinx repository.
        entry_regex = re.compile(r'(?x)(.+?)\s+(\S*:\S*)\s+(-?\d+)\s+(\S+)\s+(.*)')
        for line in stream.read_compressed_lines():
            match = entry_regex.match(line.rstrip())
            if not match:
                continue

            name, directive, prio, location, dispname = match.groups()
            domain, _, subdirective = directive.partition(':')
            if directive == 'py:module' and name in result:
                # From the Sphinx Repository:
                # due to a bug in 1.1 and below,
                # two inventory entries are created
                # for Python modules, and the first
                # one is correct
                continue

            # Most documentation pages have a label
            if directive == 'std:doc':
                subdirective = 'label'

            if location.endswith('$'):
                location = location[:-1] + name

            key = name if dispname == '-' else dispname
            prefix = f'{subdirective}:' if domain == 'std' else ''

            if projname == 'discord.py':
                key = key.replace('discord.ext.commands.', '').replace('discord.', '')

            result[f'{prefix}{key}'] = os.path.join(url, location)

        return result

    async def build_rtfm_lookup_table(self, page_types):
        cache = {}
        for key, page in page_types.items():
            sub = cache[key] = {}
            async with self.bot.session.get(page + '/objects.inv') as resp:
                if resp.status != 200:
                    raise RuntimeError('Cannot build rtfm lookup table, try again later.')

                stream = SphinxObjectFileReader(await resp.read())
                cache[key] = self.parse_object_inv(stream, page)

        self._rtfm_cache = cache

    async def do_rtfm(self, ctx, key, obj):
        page_types = {
            'latest': 'https://discordpy.readthedocs.io/en/latest',
            'python': 'https://docs.python.org/3'
        }

        if obj is None:
            await ctx.send(page_types[key])
            return

        if not hasattr(self, '_rtfm_cache'):
            await ctx.trigger_typing()
            await self.build_rtfm_lookup_table(page_types)

        obj = re.sub(r'^(?:discord\.(?:ext\.)?)?(?:commands\.)?(.+)', r'\1', obj)

        if key == 'latest':
            pit_of_success_helpers = {
                'vc': 'VoiceClient',
                'msg': 'Message',
                'color': 'Colour',
                'perm': 'Permissions',
                'channel': 'TextChannel',
                'chan': 'TextChannel',
            }

            # point the abc.Messageable types properly:
            q = obj.lower()
            for name in dir(discord.abc.Messageable):
                if name[0] == '_':
                    continue
                if q == name:
                    obj = f'abc.Messageable.{name}'
                    break

            def replace(o):
                return pit_of_success_helpers.get(o.group(1), '')

            pattern = re.compile('|'.join(fr'(?:^({k})$|({k})\.)' for k in pit_of_success_helpers.keys()))
            obj = pattern.sub(replace, obj)

        cache = list(self._rtfm_cache[key].items())
        def transform(tup):
            return tup[0]

        matches = fuzzy.finder(obj, cache, key=lambda t: t[0], lazy=False)[:8]

        e = discord.Embed(colour=discord.Colour.blurple())
        if len(matches) == 0:
            return await ctx.send('Could not find anything. Sorry.')

        e.description = '\n'.join(f'[{key}]({url})' for key, url in matches)
        await ctx.send(embed=e)

    @commands.group(aliases=['rtfd'], invoke_without_command=True)
    async def rtfm(self, ctx, *, obj: str = None):
        """Gives you a documentation link for a discord.py entity.

        Events, objects, and functions are all supported through a
        a cruddy fuzzy algorithm.
        """
        await self.do_rtfm(ctx, 'latest', obj)

    @rtfm.command(name='python', aliases=['py'])
    async def rtfm_python(self, ctx, *, obj: str = None):
        """Gives you a documentation link for a Python entity."""
        await self.do_rtfm(ctx, 'python', obj)

    async def refresh_faq_cache(self):
        self.faq_entries = {}
        base_url = 'https://discordpy.readthedocs.io/en/latest/faq.html'
        async with self.bot.session.get(base_url) as resp:
            text = await resp.text(encoding='utf-8')

            root = etree.fromstring(text, etree.HTMLParser())
            nodes = root.findall(".//div[@id='questions']/ul[@class='simple']/li/ul//a")
            for node in nodes:
                self.faq_entries[''.join(node.itertext()).strip()] = base_url + node.get('href').strip()

    @commands.command()
    async def faq(self, ctx, *, query: str = None):
        """Shows an FAQ entry from the discord.py documentation"""
        if not hasattr(self, 'faq_entries'):
            await self.refresh_faq_cache()

        if query is None:
            return await ctx.send(f'https://discordpy.readthedocs.io/en/{branch}/faq.html')

        matches = fuzzy.extract_matches(query, self._faq_cache[branch], scorer=fuzzy.partial_ratio, score_cutoff=40)
        if len(matches) == 0:
            return await ctx.send('Nothing found…')

        fmt = '\n'.join(f'**{key}**\n{value}' for key, _, value in matches)
        await ctx.send(fmt)

def setup(bot):
    bot.add_cog(API(bot))
