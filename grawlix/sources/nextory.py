from grawlix.book import Book, Metadata, OnlineFile, BookData, OnlineFile, SingleFile, EpubInParts, Result, Series
from grawlix.encryption import AESEncryption
from grawlix.exceptions import InvalidUrl
from .source import Source

from typing import Optional
import uuid
import rich
import base64

LOCALE = "en_GB"

class Nextory(Source):
    name: str = "Nextory"
    match = [
        r"https?://((www|catalog-\w\w).)?nextory.+"
    ]
    _authentication_methods = [ "login" ]


    @staticmethod
    def _create_device_id() -> str:
        """Create unique device id"""
        return str(uuid.uuid3(uuid.NAMESPACE_DNS, "audiobook-dl"))


    async def login(self, url: str, username: str, password: str) -> None:
        # Set permanent headers
        device_id = self._create_device_id()
        self._client.headers.update(
            {
                "X-Application-Id": "200",
                "X-App-Version": "2026.05.4",
                "X-Locale": LOCALE,
                "X-Model": "Personal Computer",
                "X-Device-Id": device_id,
                "X-Os-Info": "Android",
                "appid": "200",
            }
        )
        # Login for account
        session_response = await self._client.post(
            "https://api.nextory.com/user/v1/sessions",
            json = {
                "identifier": username,
                "password": password
            },
        )
        session_response = session_response.json()
        login_token = session_response["login_token"]
        country = session_response["country"]
        self._client.headers.update(
            {
                "token": login_token,
                "X-Login-Token": login_token,
                "X-Country-Code": country,
            }
        )
        # Login for user
        profiles_response = await self._client.get(
            "https://api.nextory.com/user/v1/me/profiles",
        )
        profiles_response = profiles_response.json()
        profile = profiles_response["profiles"][0]
        login_key = profile["login_key"]
        authorize_response = await self._client.post(
            "https://api.nextory.com/user/v1/profile/authorize",
            json = {
                "login_key": login_key
            }
        )
        authorize_response = authorize_response.json()
        profile_token = authorize_response["profile_token"]
        self._client.headers.update({"X-Profile-Token": profile_token})


    @staticmethod
    def _find_epub_id(product_data) -> str:
        """Find id of book format of type epub for given book"""
        for format in product_data["formats"]:
            if format["type"] == "epub":
                return format["identifier"]
        raise InvalidUrl


    @staticmethod
    def _extract_id_from_url(url: str) -> str:
        """
        Extract id of book from url. This id is not always the internal id for
        the book.

        :param url: Url to book information page
        :return: Id in url
        """
        return url.split("-")[-1].replace("/", "")


    async def download(self, url: str) -> Result:
        if "want-to-read" in url.lower() or "saved-for-later" in url.lower():
            return await self._download_want_to_read()
        url_id = self._extract_id_from_url(url)
        if "serier" in url:
            return await self._download_series(url_id)
        else:
            return await self._download_book(url_id)


    async def download_book_from_id(self, book_id: str) -> Book:
        return await self._download_book(book_id)


    async def _download_want_to_read(self) -> Series:
        """
        Download every ebook saved in the user's want-to-read ("Saved for
        later") list. Entries without an epub format (e.g. audiobook-only) are
        skipped, since grawlix downloads ebooks.

        :returns: Series of ebook ids
        """
        lists_response = await self._client.get(
            "https://api.nextory.com/library/v1/me/product_lists",
            params = { "page": 0, "per": 50 },
        )
        want_to_read_id = None
        for product_list in lists_response.json()["product_lists"]:
            if product_list["type"] == "want_to_read":
                want_to_read_id = product_list["id"]
                break
        if want_to_read_id is None:
            raise InvalidUrl
        book_ids = []
        page = 0
        while True:
            response = await self._client.get(
                "https://api.nextory.com/library/v1/me/product_lists/want_to_read/products",
                params = { "page": page, "per": 1000, "id": want_to_read_id },
            )
            products = response.json().get("products", [])
            if not products:
                break
            for product in products:
                if any(f.get("type") == "epub" for f in product.get("formats", [])):
                    book_ids.append(product["id"])
            if len(products) < 1000:
                break
            page += 1
        return Series(
            title = "Nextory - Saved for later",
            book_ids = book_ids,
        )


    async def _download_series(self, series_id: str) -> Series:
        """
        Download series from Nextory

        :param series_id: Id of series on Nextory
        :returns: Series data
        """
        response = await self._client.get(
            f"https://api.nextory.com/discovery/v1/series/{series_id}/products",
            params = {
                "content_type": "book",
                "page": 0,
                "per": 100,
            }
        )
        series_data = response.json()
        book_ids = []
        for book in series_data["products"]:
            book_id = book["id"]
            book_ids.append(book_id)
        return Series(
            title = series_data["products"][0]["series"]["name"],
            book_ids = book_ids,
        )


    @staticmethod
    def _extract_series_name(product_info: dict) -> Optional[str]:
        series = product_info.get("series")
        if not series:
            return None
        return series["name"]


    async def _get_book_id_from_url_id(self, url_id: str) -> str:
        """
        Download book id from url id

        :param url_id: Id of book from url
        :return: Book id
        """
        response = await self._client.get(
            f"https://api.nextory.se/api/app/product/7.5/bookinfo",
            params = { "id": url_id },
        )
        rich.print(response.url)
        rich.print(response.content)
        exit()


    async def _download_book(self, book_id: str) -> Book:
        product_data = await self._client.get(
            f"https://api.nextory.com/library/v1/products/{book_id}"
        )
        product_data = product_data.json()
        epub_id = self._find_epub_id(product_data)
        pages = await self._get_pages(epub_id)
        return Book(
            data = pages,
            metadata = Metadata(
                title = product_data["title"],
                authors = [author["name"] for author in product_data["authors"]],
                series = self._extract_series_name(product_data),
            )
        )


    @staticmethod
    def _fix_key(value: str) -> bytes:
        """Remove unused data and decode key"""
        return base64.b64decode(value[:-1])


    async def _get_pages(self, epub_id: str) -> BookData:
        """
        Download page information for book

        :param epub_id: Id of epub file
        :return: Page data
        """
        # Nextory books are for some reason split up into multiple epub files -
        # one for each chapter file. All of these files has to be decrypted and
        # combined afterwards. Many of the provided epub files contain the same
        # files and some of them contain the same file names but with variation
        # in the content and comments that describe what should have been there
        # if the book was whole from the start.
        response = await self._client.get(
            f"https://api.nextory.com/reader/books/{epub_id}/packages/epub"
        )
        epub_data = response.json()
        encryption = AESEncryption(
            key = self._fix_key(epub_data["crypt_key"]),
            iv = self._fix_key(epub_data["crypt_iv"])
        )
        files = []
        for part in epub_data["spines"]:
            files.append(
                OnlineFile(
                    url = part["spine_url"],
                    extension = "epub",
                    encryption = encryption
                )
            )
        files_in_toc = {}
        for item in epub_data["toc"]["childrens"]: # Why is it "childrens"?
            files_in_toc[item["src"]] = item["name"]
        return EpubInParts(
            files,
            files_in_toc
        )
