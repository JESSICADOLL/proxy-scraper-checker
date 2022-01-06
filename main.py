#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import logging
from contextlib import suppress
from ipaddress import IPv4Address
from pathlib import Path
from random import shuffle
from shutil import rmtree
from time import perf_counter
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

from aiohttp import ClientSession
from aiohttp_socks import ProxyConnector
from maxminddb import open_database
from maxminddb.reader import Reader

import config


class Proxy:
    def __init__(self, socket_address: str) -> None:
        self.SOCKET_ADDRESS = socket_address
        self._IP = socket_address.split(":")[0]
        self.exit_node: Optional[str] = None
        self.is_anonymous: Optional[bool] = None
        self.geolocation = "::None::None::None"
        self.timeout = float("inf")

    def set_anonymity(self) -> None:
        self.is_anonymous = self._IP != self.exit_node

    def set_geolocation(self, reader: Reader) -> None:
        geolocation = reader.get(self._IP)
        if not isinstance(geolocation, dict):
            return
        country = geolocation.get("country")
        if country:
            country = country["names"]["en"]
        else:
            country = geolocation.get("continent")
            if country:
                country = country["names"]["en"]
        region = geolocation.get("subdivisions")
        if region:
            region = region[0]["names"]["en"]
        city = geolocation.get("city")
        if city:
            city = city["names"]["en"]
        self.geolocation = f"::{country}::{region}::{city}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Proxy):
            return NotImplemented
        return self.SOCKET_ADDRESS == other.SOCKET_ADDRESS

    def __hash__(self) -> int:
        return hash(("socket_address", self.SOCKET_ADDRESS))


class ProxyScraperChecker:
    def __init__(
        self,
        *,
        timeout: float = 10,
        max_connections: int = 950,
        sort_by_speed: bool = True,
        geolite2_city_mmdb: Optional[str] = None,
        ip_service: str = "https://checkip.amazonaws.com",
        save_path: str = "",
        http_sources: Optional[Iterable[str]] = None,
        socks4_sources: Optional[Iterable[str]] = None,
        socks5_sources: Optional[Iterable[str]] = None,
    ) -> None:
        """HTTP, SOCKS4, SOCKS5 proxies scraper and checker.

        Args:
            timeout (float): How many seconds to wait for the connection.
            max_connections (int): Maximum concurrent connections.
            sort_by_speed (bool): Set to False to sort proxies alphabetically.
            geolite2_city_mmdb (str): Path to the GeoLite2-City.mmdb if you
                want to add location info for each proxy.
            ip_service (str): Service for getting your IP address and checking
                if proxies are valid.
            save_path (str): Path to the folder where the proxy folders will be
                saved.
        """
        self.sem = asyncio.Semaphore(max_connections)
        self.IP_SERVICE = ip_service.strip()
        self.SORT_BY_SPEED = sort_by_speed
        self.TIMEOUT = timeout
        self.PATH = save_path
        self.MMDB = geolite2_city_mmdb
        self.SOURCES = {
            proto: (sources,)
            if isinstance(sources, str)
            else frozenset(sources)
            for proto, sources in (
                ("http", http_sources),
                ("socks4", socks4_sources),
                ("socks5", socks5_sources),
            )
            if sources
        }
        self.proxies: Dict[str, List[Proxy]] = {
            proto: [] for proto in self.SOURCES
        }
        self.proxies_count = {proto: 0 for proto in self.SOURCES}

    async def fetch_source(
        self, session: ClientSession, source: str, proto: str
    ) -> None:
        """Get proxies from source.

        Args:
            source (str): Proxy list URL.
            proto (str): http/socks4/socks5.
        """
        try:
            async with session.get(source.strip(), timeout=15) as r:
                status = r.status
                text = await r.text(encoding="utf-8")
        except Exception as e:
            logging.error("%s: %s", source, e)
        else:
            if status == 200:
                for proxy in text.splitlines():
                    proxy = (
                        proxy.replace(f"{proto}://", "")
                        .replace("https://", "")
                        .strip()
                    )
                    try:
                        IPv4Address(proxy.split(":")[0])
                    except Exception:
                        continue
                    self.proxies[proto].append(Proxy(proxy))
            else:
                logging.error("%s status code: %s", source, status)

    async def check_proxy(self, proxy: Proxy, proto: str) -> None:
        """Check proxy validity.

        Args:
            proxy (Proxy): ip:port.
            proto (str): http/socks4/socks5.
        """
        try:
            async with self.sem:
                start = perf_counter()
                async with ClientSession(
                    connector=ProxyConnector.from_url(
                        f"{proto}://{proxy.SOCKET_ADDRESS}"
                    )
                ) as session:
                    async with session.get(
                        self.IP_SERVICE, timeout=self.TIMEOUT
                    ) as r:
                        exit_node = await r.text(encoding="utf-8")
            proxy.timeout = perf_counter() - start
            exit_node = exit_node.strip()
            IPv4Address(exit_node)
        except Exception as e:

            # Too many open files
            if isinstance(e, OSError) and e.errno == 24:
                logging.error("Please, set MAX_CONNECTIONS to lower value.")

            self.proxies[proto].remove(proxy)
        else:
            proxy.exit_node = exit_node
            proxy.set_anonymity()

    async def fetch_all_sources(self) -> None:
        """Get proxies from sources."""
        logging.info("Fetching sources")
        async with ClientSession() as session:
            coroutines = (
                self.fetch_source(session, source, proto)
                for proto, sources in self.SOURCES.items()
                for source in sources
            )
            await asyncio.gather(*coroutines)

        # Remove duplicates
        for proto in self.proxies:
            self.proxies[proto] = list(frozenset(self.proxies[proto]))

        # Remember total count so we could print it in the table
        for proto, proxies in self.proxies.items():
            self.proxies_count[proto] = len(proxies)

    async def check_all_proxies(self) -> None:
        for proto, proxies in self.proxies.items():
            logging.info("Checking %s %s proxies", len(proxies), proto)
        coroutines = [
            self.check_proxy(proxy, proto)
            for proto, proxies in self.proxies.items()
            for proxy in proxies
        ]
        shuffle(coroutines)
        await asyncio.gather(*coroutines)

    def set_geolocation(self) -> None:
        if not self.MMDB:
            return
        with open_database(self.MMDB) as reader:
            for proxies in self.proxies.values():
                for proxy in proxies:
                    proxy.set_geolocation(reader)

    def sort_proxies(self) -> None:
        key = self._sorting_key
        for proto in self.proxies:
            self.proxies[proto].sort(key=key)

    def save_proxies(self) -> None:
        """Delete old proxies and save new ones."""
        path = Path(self.PATH)
        dirs = tuple(
            path / dir
            for dir in (
                "proxies",
                "proxies_anonymous",
                "proxies_geolocation",
                "proxies_geolocation_anonymous",
            )
        )
        for dir in dirs:
            with suppress(FileNotFoundError):
                rmtree(dir)

        # proxies and proxies_anonymous folders
        for dir in dirs[:2]:
            dir.mkdir(parents=True, exist_ok=True)
        for proto, proxies in self.proxies.items():
            file_name = f"{proto}.txt"

            text = "\n".join(proxy.SOCKET_ADDRESS for proxy in proxies)
            (dirs[0] / file_name).write_text(text, encoding="utf-8")

            anon_text = "\n".join(
                proxy.SOCKET_ADDRESS for proxy in proxies if proxy.is_anonymous
            )
            (dirs[1] / file_name).write_text(anon_text, encoding="utf-8")

        # proxies_geolocation and proxies_geolocation_anonymous folders
        if not self.MMDB:
            return
        self.set_geolocation()
        for dir in dirs[-2:]:
            dir.mkdir(parents=True, exist_ok=True)
        for proto, proxies in self.proxies.items():
            file_name = f"{proto}.txt"

            text = "\n".join(
                f"{proxy.SOCKET_ADDRESS}{proxy.geolocation}"
                for proxy in proxies
            )
            (dirs[2] / file_name).write_text(text, encoding="utf-8")

            anon_text = "\n".join(
                f"{proxy.SOCKET_ADDRESS}{proxy.geolocation}"
                for proxy in proxies
                if proxy.is_anonymous
            )
            (dirs[3] / file_name).write_text(anon_text, encoding="utf-8")

    async def main(self) -> None:
        await self.fetch_all_sources()
        await self.check_all_proxies()

        logging.info("Result:")
        for proto, proxies in self.proxies.items():
            logging.info("%s - %s", proto, len(proxies))

        self.sort_proxies()
        self.save_proxies()

        logging.info(
            "Proxy folders have been created in the %s.",
            f"{self.PATH} folder" if self.PATH else "current directory",
        )
        logging.info("Thank you for using proxy-scraper-checker :)")

    @property
    def _sorting_key(self) -> Callable[[Proxy], Union[float, Tuple[int, ...]]]:
        if self.SORT_BY_SPEED:
            return lambda proxy: proxy.timeout
        return lambda proxy: tuple(
            map(int, proxy.SOCKET_ADDRESS.replace(":", ".").split("."))
        )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    await ProxyScraperChecker(
        timeout=config.TIMEOUT,
        max_connections=config.MAX_CONNECTIONS,
        sort_by_speed=config.SORT_BY_SPEED,
        geolite2_city_mmdb=(
            "GeoLite2-City.mmdb" if config.GEOLOCATION else None
        ),
        ip_service=config.IP_SERVICE,
        save_path=config.SAVE_PATH,
        http_sources=config.HTTP_SOURCES if config.HTTP else None,
        socks4_sources=config.SOCKS4_SOURCES if config.SOCKS4 else None,
        socks5_sources=config.SOCKS5_SOURCES if config.SOCKS5 else None,
    ).main()


if __name__ == "__main__":
    asyncio.run(main())
