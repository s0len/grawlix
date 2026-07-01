from grawlix.book import HtmlFiles, HtmlFile, OnlineFile, Book, SingleFile, Metadata, EpubInParts
from grawlix.exceptions import UnsupportedOutputFormat
from .output_format import OutputFormat, Update

import asyncio
from bs4 import BeautifulSoup
import html as _html
import os
import re
from ebooklib import epub
from zipfile import ZipFile, ZipInfo, ZIP_STORED, ZIP_DEFLATED
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
        # Make the file self-describing regardless of source (Nextory rebuilds
        # the epub via ebooklib and would otherwise embed no title/author/series;
        # Storytel keeps the publisher epub which lacks series info).
        self._embed_metadata(location, book.metadata)


    @staticmethod
    def _embed_metadata(location: str, metadata: Metadata) -> None:
        """
        Best-effort: add missing identity/series metadata to the epub's OPF so
        the file is self-describing regardless of source. Only fields that are
        ABSENT are added — existing OPF content is never removed or rewritten,
        which keeps publisher metadata, EPUB3 ``refines``, comments and foreign
        namespaces intact. Any failure is swallowed: embedding is an enhancement
        and must never break an otherwise successful download.
        """
        try:
            with ZipFile(location) as zf:
                names = set(zf.namelist())
                if "META-INF/container.xml" not in names:
                    return
                container = zf.read("META-INF/container.xml").decode("utf-8")
                m = re.search(r"full-path\s*=\s*['\"]([^'\"]+)['\"]", container)
                if not m or m.group(1) not in names:
                    return
                opf_name = m.group(1)
                raw = zf.read(opf_name)

            # Respect the OPF's declared encoding; bail if we cannot round-trip it.
            encoding = "utf-8"
            decl = re.match(rb"<\?xml[^>]*?encoding=['\"]([\w.-]+)['\"]", raw)
            if decl:
                encoding = decl.group(1).decode("ascii", "ignore") or "utf-8"
            try:
                opf = raw.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                return

            close = re.search(r"</\s*metadata\s*>", opf)
            if not close:
                return

            def esc(value: object) -> str:
                return _html.escape(str(value), quote=True)

            def missing(pattern: str) -> bool:
                return re.search(pattern, opf) is None

            inject: list[str] = []
            if metadata.title and missing(r"<dc:title[\s/>]"):
                inject.append(f"<dc:title>{esc(metadata.title)}</dc:title>")
            if metadata.authors and missing(r"<dc:creator[\s/>]"):
                inject += [f"<dc:creator>{esc(a)}</dc:creator>" for a in metadata.authors]
            if metadata.language and missing(r"<dc:language[\s/>]"):
                inject.append(f"<dc:language>{esc(metadata.language)}</dc:language>")
            if metadata.publisher and missing(r"<dc:publisher[\s/>]"):
                inject.append(f"<dc:publisher>{esc(metadata.publisher)}</dc:publisher>")
            if metadata.release_date and missing(r"<dc:date[\s/>]"):
                inject.append(f"<dc:date>{esc(metadata.release_date)}</dc:date>")
            if metadata.series and missing(r"""(?:name=['"]calibre:series['"]|belongs-to-collection)"""):
                inject.append(f'<meta name="calibre:series" content="{esc(metadata.series)}"/>')
                if metadata.index is not None:
                    inject.append(f'<meta name="calibre:series_index" content="{esc(metadata.index)}"/>')
            if not inject:
                return

            opf = opf[:close.start()] + "".join(inject) + opf[close.start():]
            new_opf = opf.encode(encoding)

            tmp = f"{location}.tmp"
            try:
                with ZipFile(location) as zin, ZipFile(tmp, "w") as zout:
                    for item in zin.infolist():  # preserves entry order (mimetype first)
                        data = new_opf if item.filename == opf_name else zin.read(item)
                        compress = ZIP_STORED if item.filename == "mimetype" else ZIP_DEFLATED
                        info = ZipInfo(item.filename, date_time=item.date_time)
                        info.compress_type = compress
                        info.external_attr = item.external_attr
                        zout.writestr(info, data)
                os.replace(tmp, location)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except Exception:
            # Never let metadata embedding break a successful download.
            return


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
        file_count = len(files)
        progress = 1/(file_count)
        temporary_file_location = f"{location}.tmp"

        added_files: set[str] = set()
        def get_new_files(zipfile: ZipFile):
            """Returns files in zipfile not already added to file"""
            for filename in zipfile.namelist():
                if filename in added_files or filename.endswith(".opf") or filename.endswith(".ncx"):
                    continue
                yield filename

        output = epub.EpubBook()
        for file in files:
            await self._download_and_write_file(file, temporary_file_location)
            with ZipFile(temporary_file_location, "r") as zipfile:
                for filepath in get_new_files(zipfile):
                    content = zipfile.read(filepath)
                    if filepath.endswith("html"):
                        filename = os.path.basename(filepath)
                        is_in_toc = False
                        title = None
                        for key, value in data.files_in_toc.items():
                            toc_filename = key.split("#")[0]
                            if filename == toc_filename:
                                title = value
                                is_in_toc = True
                                break
                        epub_file = epub.EpubHtml(
                            title = title,
                            file_name = filepath,
                            content = content
                        )
                        output.add_item(epub_file)
                        output.spine.append(epub_file)
                        if is_in_toc:
                            output.toc.append(epub_file)
                    else:
                        epub_file = epub.EpubItem(
                            file_name = filepath,
                            content = content
                        )
                        output.add_item(epub_file)
                    added_files.add(filepath)
            if update:
                update(progress)
        os.remove(temporary_file_location)

        output.add_item(epub.EpubNcx())
        output.add_item(epub.EpubNav())
        epub.write_epub(location, output)
