#!/usr/bin/env python3
"""
Конвертер экспорта Joplin (RAW - Joplin Export Directory) в Obsidian vault.

Использование:
    python3 joplin_to_obsidian.py /путь/к/RAW-экспорту /путь/к/выходной/папке

Что делает:
- Разбирает все .md-файлы raw-экспорта на (title, body, metadata)
- Строит дерево блокнотов (type_: 2) -> путь папок
- Строит карту id ресурса (type_: 4) -> реальное имя файла с расширением
- Строит карту id тега (type_: 5) и связей заметка-тег (type_: 6)
- Для заметок с markup_language: 2 (HTML, обычно после CherryTree/Evernote)
  конвертирует HTML в Markdown через markdownify
- Заменяет ссылки вида :/<32-символьный-id> на:
    - ![[attachments/имя_файла]] для картинок/файлов
    - [[Название заметки]] для ссылок на другие заметки
- Добавляет заголовок (frontmatter) с тегами и датой создания
- Копирует файлы ресурсов в attachments/
"""

import sys
import re
import shutil
import urllib.parse
from pathlib import Path

ID_RE = re.compile(r'^[0-9a-f]{32}$')

MAX_FILENAME_LEN = 150  # с запасом под ограничения Windows (полный путь < 260)


def decode_resource_title(title: str) -> str:
    """Joplin иногда хранит title ресурса в application/x-www-form-urlencoded виде:
    пробелы как '+', кириллица как %D0%90 и т.п. Декодируем обратно."""
    if "%" in title or "+" in title:
        try:
            decoded = urllib.parse.unquote_plus(title)
            if decoded:
                title = decoded
        except Exception:
            pass
    return title


def build_resource_filename(res_title: str, file_extension: str) -> str:
    """Строит имя файла ресурса без задвоения расширения (foo.JPG.jpg -> foo.JPG)."""
    title = decode_resource_title(res_title)
    title = sanitize_filename(title)
    if file_extension:
        ext_dot = "." + file_extension
        if title.lower().endswith(ext_dot.lower()):
            fname = title
        else:
            fname = title + ext_dot
    else:
        fname = title
    return truncate_filename(fname)


def truncate_filename(fname: str) -> str:
    if len(fname) <= MAX_FILENAME_LEN:
        return fname
    stem = Path(fname).stem
    suffix = Path(fname).suffix
    keep = MAX_FILENAME_LEN - len(suffix)
    return stem[:keep] + suffix


def parse_raw_note(path: Path):
    """Разбирает один .md файл raw-экспорта Joplin на title, body, meta(dict)."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")

    # metadata: снизу вверх, пока строки похожи на "ключ: значение"
    meta_start = len(lines)
    i = len(lines) - 1
    # идём с конца, пропуская финальные пустые строки
    while i >= 0 and lines[i].strip() == "":
        i -= 1
    meta_end = i + 1  # конец метаданных (не включительно), обычно = len(lines)
    KEY_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*:( .*)?$')
    while i >= 0 and (lines[i].strip() == "" or KEY_RE.match(lines[i])):
        if lines[i].strip() == "":
            # пустая строка внутри блока метаданных недопустима у Joplin,
            # значит это разделитель между телом и метаданными
            break
        i -= 1
    meta_start = i + 1

    meta_lines = lines[meta_start:meta_end]
    meta = {}
    for line in meta_lines:
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()

    # между телом и метаданными должна быть пустая строка - уберём её
    body_end = meta_start
    while body_end > 0 and lines[body_end - 1].strip() == "":
        body_end -= 1

    title = lines[0] if lines else ""
    body = "\n".join(lines[2:body_end]) if len(lines) > 2 else ""

    return title, body, meta


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name or "untitled"


def convert_html_to_md(html: str) -> str:
    from markdownify import markdownify as md
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    if soup.head:
        soup.head.decompose()  # убираем <title>/<meta>/<link> - это не контент заметки
    body_tag = soup.body if soup.body else soup
    return md(str(body_tag), heading_style="ATX")


def main(src_dir: str, out_dir: str):
    src = Path(src_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    attachments_dir = out / "attachments"
    attachments_dir.mkdir(exist_ok=True)

    notes, folders, resources, tags, note_tags = {}, {}, {}, {}, []

    md_files = list(src.rglob("*.md"))
    print(f"Найдено {len(md_files)} .md файлов в raw-экспорте")

    for f in md_files:
        title, body, meta = parse_raw_note(f)
        type_ = meta.get("type_")
        mid = meta.get("id")
        if not mid:
            continue
        if type_ == "1":
            notes[mid] = {"title": title, "body": body, "meta": meta, "path": f}
        elif type_ == "2":
            folders[mid] = {"title": title, "parent_id": meta.get("parent_id", "")}
        elif type_ == "4":
            # ресурс: файл с бинарным содержимым лежит рядом,
            # обычно в подпапке resources/ с тем же id + расширением из meta
            ext = meta.get("file_extension", "")
            resources[mid] = {"title": title, "ext": ext}
        elif type_ == "5":
            tags[mid] = title
        elif type_ == "6":
            note_tags.append((meta.get("note_id"), meta.get("tag_id")))

    # найдём реальные файлы ресурсов на диске (Joplin кладёт их в .resources/ или resources/)
    resource_files = {}
    for cand_dir in [src / ".resources", src / "resources"]:
        if cand_dir.exists():
            for rf in cand_dir.iterdir():
                if rf.is_file():
                    resource_files[rf.stem] = rf

    # путь блокнота -> Path
    def folder_path(fid):
        parts = []
        seen = set()
        while fid and fid in folders and fid not in seen:
            seen.add(fid)
            parts.append(sanitize_filename(folders[fid]["title"]))
            fid = folders[fid]["parent_id"]
        return Path(*reversed(parts)) if parts else Path(".")

    # id заметки -> итоговое имя файла (с учётом дублей заголовков)
    used_names = {}
    note_filename = {}
    for nid, n in notes.items():
        base = truncate_filename(sanitize_filename(n["title"]) + ".md")[:-3]
        name = base
        count = 1
        key = (str(folder_path(n["meta"].get("parent_id", ""))), name)
        while key in used_names:
            count += 1
            name = f"{base} ({count})"
            key = (str(folder_path(n["meta"].get("parent_id", ""))), name)
        used_names[key] = nid
        note_filename[nid] = name + ".md"

    # id заметки -> её теги
    note_id_to_tags = {}
    for note_id, tag_id in note_tags:
        if note_id and tag_id in tags:
            note_id_to_tags.setdefault(note_id, []).append(tags[tag_id])

    # id ресурса -> итоговое уникальное имя файла (без коллизий между собой)
    resource_filename = {}
    used_resource_names = set()
    for rid, res in resources.items():
        base = build_resource_filename(res["title"], res["ext"])
        name = base
        count = 1
        while name in used_resource_names:
            count += 1
            stem, suffix = Path(base).stem, Path(base).suffix
            name = f"{stem} ({count}){suffix}"
        used_resource_names.add(name)
        resource_filename[rid] = name

    link_re = re.compile(r':/([0-9a-fA-F]{32})')

    def replace_links(text, is_html):
        def repl(m):
            target_id = m.group(1)
            if target_id in resource_filename:
                return f"attachments/{resource_filename[target_id]}"
            elif target_id in notes:
                target_name = note_filename[target_id][:-3]  # без .md
                if is_html:
                    return f"[[{target_name}]]"
                return f"{target_name}"
            return m.group(0)
        return link_re.sub(repl, text)

    # копируем ресурсы в attachments/ под их реальными названиями
    copy_errors = 0
    for rid, res in resources.items():
        fname = resource_filename[rid]
        src_file = resource_files.get(rid)
        if not src_file:
            print(f"  ! Не найден файл ресурса на диске для id={rid} ({res['title']})")
            continue
        try:
            shutil.copy2(src_file, attachments_dir / fname)
        except OSError as e:
            copy_errors += 1
            print(f"  ! Ошибка копирования {src_file.name} -> {fname}: {e}")
    if copy_errors:
        print(f"  Не скопировано ресурсов из-за ошибок: {copy_errors} "
              f"(вероятно, слишком длинный путь Windows - попробуйте перенести "
              f"выходную папку ближе к корню диска, напр. C:\\obsidian)")

    # пишем заметки
    note_errors = 0
    for nid, n in notes.items():
      try:
        meta = n["meta"]
        body = n["body"]
        is_html = meta.get("markup_language") == "2"

        if is_html:
            # ссылки лежат в src=/href=":/id" - подменим их ДО конвертации в markdown,
            # чтобы markdownify сам превратил <a href="target"> и <img src="target">
            # в нормальные markdown-ссылки/картинки
            def html_repl(m):
                target_id = m.group(1)
                if target_id in resource_filename:
                    return f"attachments/{resource_filename[target_id]}"
                elif target_id in notes:
                    target_name = note_filename[target_id][:-3]
                    return f"{target_name}.md"
                return m.group(0)
            body = link_re.sub(html_repl, body)
            body = convert_html_to_md(body)
        else:
            body = replace_links(body, is_html=False)

        note_tags_list = note_id_to_tags.get(nid, [])
        fm_lines = ["---"]
        fm_lines.append(f"created: {meta.get('user_created_time', meta.get('created_time',''))}")
        if note_tags_list:
            tags_str = ", ".join(sanitize_filename(t).replace(" ", "-") for t in note_tags_list)
            fm_lines.append(f"tags: [{tags_str}]")
        fm_lines.append("---\n")
        frontmatter = "\n".join(fm_lines)

        rel_folder = folder_path(meta.get("parent_id", ""))
        target_dir = out / rel_folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / note_filename[nid]
        target_file.write_text(frontmatter + body.strip() + "\n", encoding="utf-8")
      except Exception as e:
        note_errors += 1
        print(f"  ! Ошибка обработки заметки id={nid} ({n.get('title','')}): {e}")

    print(f"Готово. Заметок: {len(notes)} (ошибок: {note_errors}), блокнотов: {len(folders)}, "
          f"ресурсов найдено на диске: {len(resource_files)} из {len(resources)} упомянутых, "
          f"тегов: {len(tags)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: python3 joplin_to_obsidian.py <raw_export_dir> <output_dir>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
