from grawlix.book import HtmlFiles, HtmlFile, OnlineFile, Book, SingleFile, Metadata, EpubInParts
from grawlix.exceptions import UnsupportedOutputFormat
from .output_format import OutputFormat, Update

import asyncio
from bs4 import BeautifulSoup
import os
from ebooklib import epub
from zipfile import ZipFile
import rich

class Epub(OutputFormat):
    extension = "epub"
    input_types = [SingleFile, HtmlFiles, EpubInParts]


    async def download(self, book: Book, location: str, update: Update) -> None:
        if isinstance(book.data, SingleFile):
            await self._download_single_file(book, location, update)
        elif isinstance(book.data, HtmlFiles):
            await self._download_html_files(book.data, book.metadata, location, update)
        elif isinstance(book.data, EpubInParts):
            await self._download_epub_in_parts(book.data, book.metadata, location, update)
        else:
            raise UnsupportedOutputFormat


    async def _download_html_files(self, html: HtmlFiles, metadata: Metadata, location: str, update: Update) -> None:
        output = epub.EpubBook()
        output.set_title(metadata.title)
        for author in metadata.authors:
            output.add_author(author)
        file_count = len(html.htmlfiles) + 1 # Html files + cover

        async def download_cover(cover_file: OnlineFile):
            cover_filename = f"cover.{cover_file.extension}"
            epub_cover = epub.EpubCover(file_name = cover_filename)
            epub_cover.content = await self._download_file(cover_file)
            output.add_item(epub_cover)
            epub_cover_page = epub.EpubCoverHtml(image_name = cover_filename)
            if update:
                update(1/file_count)
            return epub_cover_page


        async def download_file(index: int, file: HtmlFile):
            response = await self._client.get(
                file.file.url,
                headers = file.file.headers,
                cookies = file.file.cookies,
                follow_redirects=True
            )
            soup = BeautifulSoup(response.text, "lxml")
            selected_element = soup.find(attrs=file.selector)
            epub_file = epub.EpubHtml(
                title = file.title,
                file_name = f"part {index}.html",
                content = str(selected_element)
            )
            if update:
                update(1/file_count)
            return epub_file

        # Download files
        tasks = [
            download_file(index, file)
            for index, file in enumerate(html.htmlfiles)
        ]
        if html.cover:
            tasks.append(download_cover(html.cover))
        epub_files = await asyncio.gather(*tasks)

        # Add files to epub
        for epub_file in epub_files:
            output.add_item(epub_file)
            output.spine.append(epub_file)
            output.toc.append(epub_file)

        # Complete book
        output.add_item(epub.EpubNcx())
        output.add_item(epub.EpubNav())
        epub.write_epub(location, output)


    async def _download_epub_in_parts(self, data: EpubInParts, metadata: Metadata, location: str, update: Update) -> None:
        files = data.files
        progress = 1/len(files)
        temporary_file_location = f"{location}.tmp"

        # First occurrence wins; parts arrive in reading order and share most
        # files, so insertion order doubles as spine order.
        collected: dict[str, bytes] = {}
        try:
            for file in files:
                await self._download_and_write_file(file, temporary_file_location)
                with ZipFile(temporary_file_location, "r") as zipfile:
                    for filepath in zipfile.namelist():
                        if filepath.endswith("/") or filepath in collected or self._is_part_packaging(filepath):
                            continue
                        collected[filepath] = zipfile.read(filepath)
                if update:
                    update(progress)
        finally:
            if os.path.exists(temporary_file_location):
                os.remove(temporary_file_location)

        self._build_epub_from_files(collected, data.files_in_toc, metadata, location)


    @staticmethod
    def _is_part_packaging(filename: str) -> bool:
        """
        True for files that belong to a part's own epub packaging. Each part is
        a complete epub, so copying these into the merged book would leave
        stray mimetype/container/package files next to the generated ones.
        """
        lowered = filename.lower()
        return (
            lowered == "mimetype"
            or lowered.startswith("meta-inf/")
            or lowered.endswith((".opf", ".ncx"))
        )


    @staticmethod
    def _build_epub_from_files(
        files: dict[str, bytes],
        files_in_toc: dict[str, str],
        metadata: Metadata,
        location: str,
    ) -> None:
        """
        Assemble an epub from the raw files collected from its parts. Content
        documents are stored byte-for-byte as delivered by the source, so their
        stylesheet links, language attributes and markup survive; only the
        packaging (opf, ncx, nav) is generated.
        """
        output = epub.EpubBook()
        output.set_title(metadata.title)
        if metadata.language:
            output.set_language(metadata.language)
        for author in metadata.authors:
            output.add_author(author)

        # Toc keys may or may not carry directory prefixes and fragments
        # ("Text/chap01.html#nav_1"); compare by basename like the spine files.
        toc_titles = {
            os.path.basename(key.split("#")[0]): title
            for key, title in files_in_toc.items()
        }
        cover_flagged = False
        for filepath, content in files.items():
            is_content_document = filepath.lower().endswith((".html", ".xhtml", ".htm"))
            item = epub.EpubItem(
                file_name = filepath,
                # Empty media type makes ebooklib guess from the extension
                media_type = "application/xhtml+xml" if is_content_document else "",
                content = content,
            )
            output.add_item(item)
            if is_content_document:
                output.spine.append(item)
                title = toc_titles.get(os.path.basename(filepath))
                if title is not None:
                    output.toc.append(epub.Link(filepath, title, uid=item.get_id()))
            elif not cover_flagged and os.path.basename(filepath).lower().startswith("cover") \
                    and item.media_type.startswith("image/"):
                item.properties = ["cover-image"]
                output.add_metadata(None, "meta", "", {"name": "cover", "content": item.get_id()})
                cover_flagged = True

        output.add_item(epub.EpubNcx())
        output.add_item(epub.EpubNav())
        epub.write_epub(location, output)
