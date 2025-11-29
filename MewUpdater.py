import os
import sys
import json
import re
import zipfile
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from io import BytesIO
import customtkinter as ctk
from tkinter import filedialog, messagebox
from PIL import Image, ImageDraw, ImageFont, ImageTk
APP_NAME = "MewUpdater"
SUFFIX = "-mewupdated"
PINK = "#ff7ab6"
PINK_HOVER = "#ff9fcf"
GRADIENT_LEFT = (255, 255, 255)
GRADIENT_RIGHT = (255, 122, 182)
MCMETA_TEXT_COMPONENT = {
    "text": "",
    "extra": [
        {"text":"U","color":"#F7EAF8"},{"text":"p","color":"#F5E4F8"},{"text":"d","color":"#F3DEF9"},{"text":"a","color":"#F1D8F9"},
        {"text":"t","color":"#EFD2F9"},{"text":"e","color":"#EDCCFA"},{"text":"d","color":"#EBC6FA"},{"text":" "},
        {"text":"w","color":"#E7BAFB"},{"text":"i","color":"#E5B4FB"},{"text":"t","color":"#E3AEFB"},{"text":"h","color":"#E1A8FC"},{"text":" "},
        {"text":"M","color":"#DD9CFC"},{"text":"e","color":"#DB96FC"},{"text":"w","color":"#D990FD"},{"text":"U","color":"#D78AFD"},
        {"text":"p","color":"#D584FD"},{"text":"d","color":"#D37EFE"},{"text":"a","color":"#D178FE"},{"text":"t","color":"#CF72FE"},
        {"text":"e","color":"#CD6CFF"},{"text":"r","color":"#CB66FF"}
    ]
}
def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def safe_mkdir(p):
    os.makedirs(p, exist_ok=True)
def write_text_file(path, text):
    safe_mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
def write_json_file(path, obj):
    safe_mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
def extract_zip_to_dir(zip_path, dest_dir):
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dest_dir)
def create_zip_from_dir(src_dir, out_zip):
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(src_dir):
            for f in files:
                full = os.path.join(root, f)
                arc = os.path.relpath(full, start=src_dir).replace(os.sep, "/")
                z.write(full, arc)
def read_pack_mcmeta(packdir):
    p = os.path.join(packdir, "pack.mcmeta")
    if not os.path.isfile(p):
        return None
    try:
        return json.load(open(p, "r", encoding="utf-8"))
    except Exception:
        return None
SLICER_TXT = resource_path("slicer.txt")
SLICER_INPUT_RE = re.compile(r'input\(\s*"([^"]+)"\s*,', re.IGNORECASE)
OUTPUT_FILE_RE = re.compile(r'new\s+OutputFile\(\s*"([^"]+)"\s*,\s*new\s+Box\(\s*([0-9\s,]+)\s*\)', re.IGNORECASE)
BOX_NUMS_RE = re.compile(r'(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)')
METADATA_START_RE = re.compile(r'\.metadata\(\s*("""|")')
METADATA_TRIPLE_RE = re.compile(r'\.metadata\(\s*("""\s*$(.*?)^\s*"""\s*\))', re.DOTALL | re.MULTILINE)
METADATA_SINGLE_RE = re.compile(r'\.metadata\(\s*"([^"]+)"\s*\)', re.DOTALL)
def load_slicer_mappings(slicer_path):
    """
    Parse slicer.txt and return a dict:
      { input_path: [ (output_path, (x,y,w,h,refW,refH), metadata_json_or_None), ... ] }
    """
    mappings = {}
    if not os.path.isfile(slicer_path):
        return mappings
    text = open(slicer_path, "r", encoding="utf-8").read()
    pos = 0
    while True:
        m = SLICER_INPUT_RE.search(text, pos)
        if not m:
            break
        input_path = m.group(1)
        start = m.end()
        next_input = SLICER_INPUT_RE.search(text, start)
        block_end = next_input.start() if next_input else len(text)
        block = text[start:block_end]
        outputs = []
        for out_match in OUTPUT_FILE_RE.finditer(block):
            out_path = out_match.group(1)
            box_args = out_match.group(2)
            bn = BOX_NUMS_RE.search(box_args)
            if bn:
                x,y,w,h,refW,refH = map(int, bn.groups())
            else:
                continue
            metadata = None
            after_pos = out_match.end()
            triple = METADATA_TRIPLE_RE.search(block, after_pos, re.DOTALL)
            single = METADATA_SINGLE_RE.search(block, after_pos, re.DOTALL)
            if triple and triple.start() == triple.start():
                metadata = triple.group(2).strip()
            elif single:
                metadata = single.group(1).strip()
            outputs.append((out_path, (x,y,w,h,refW,refH), metadata))
        if outputs:
            mappings[input_path] = outputs
        pos = block_end
    return mappings
def apply_slicer_mappings(pack_root, mappings, log, ui_progress_step):
    """
    For each mapping input -> outputs, locate the input image under pack_root (allowing realms namespace),
    load the image, crop scaled boxes and write outputs (creating folders).
    ui_progress_step should be a callable to increment UI progress.
    """
    created = 0
    for in_path, out_list in mappings.items():
        candidates = [
            os.path.join(pack_root, in_path.replace("/", os.sep)),
            os.path.join(pack_root, in_path.replace("/", os.sep).replace("assets\\realms", "assets\\realms"))
        ]
        img_file = None
        for c in candidates:
            if os.path.isfile(c):
                img_file = c
                break
        if not img_file:
            base = os.path.basename(in_path)
            for root, dirs, files in os.walk(os.path.join(pack_root, "assets")):
                if base in files:
                    img_file = os.path.join(root, base)
                    break
        if not img_file:
            log.append(f"{now_str()} — SLICER: input image not found: {in_path}")
            ui_progress_step()
            continue
        try:
            img = Image.open(img_file).convert("RGBA")
        except Exception as e:
            log.append(f"{now_str()} — SLICER: failed to open {img_file}: {e}")
            ui_progress_step()
            continue
        iw, ih = img.size
        for out_path, box, metadata in out_list:
            x,y,w,h,refW,refH = box
            rx = int(round(x * iw / refW))
            ry = int(round(y * ih / refH))
            rww = int(round(w * iw / refW))
            rhh = int(round(h * ih / refH))
            rx = max(0, min(rx, iw-1))
            ry = max(0, min(ry, ih-1))
            rww = max(1, min(rww, iw-rx))
            rhh = max(1, min(rhh, ih-ry))
            crop = img.crop((rx, ry, rx + rww, ry + rhh))
            out_full = os.path.join(pack_root, out_path.replace("/", os.sep))
            safe_mkdir(os.path.dirname(out_full))
            target_w = int(refW * rww / w) if False else rww
            try:
                crop.save(out_full)
                created += 1
                log.append(f"{now_str()} — Wrote sprite: {out_full}")
                if metadata:
                    meta_text = metadata.strip()
                    meta_text = re.sub(r'^\s*"""', '', meta_text)
                    meta_text = re.sub(r'"""\s*$', '', meta_text)
                    meta_path = out_full + ".mcmeta"
                    try:
                        json.loads(meta_text)
                        with open(meta_path, "w", encoding="utf-8") as mf:
                            mf.write(meta_text)
                        log.append(f"{now_str()} — Wrote sprite metadata: {meta_path}")
                    except Exception:
                        with open(meta_path, "w", encoding="utf-8") as mf:
                            mf.write(meta_text)
                        log.append(f"{now_str()} — Wrote raw sprite metadata: {meta_path}")
            except Exception as e:
                log.append(f"{now_str()} — Failed writing sprite {out_full}: {e}")
            ui_progress_step()
    return created
ARMOR_LAYER_1_RE = re.compile(r"(?P<mat>.+?)_layer_1(\.png)$", re.IGNORECASE)
ARMOR_LAYER_2_RE = re.compile(r"(?P<mat>.+?)_layer_2(\.png)$", re.IGNORECASE)
LEATHER_OVERLAY_RE = re.compile(r"leather_layer_(?P<n>[12])_overlay(\.png)$", re.IGNORECASE)
def ensure_skeleton(root):
    base = os.path.join(root, "assets", "minecraft")
    dirs = [
        "atlases","blockstates","equipment","font/include","items","lang","models/block","models/item",
        "particles","post_effect","shaders/core","shaders/include","shaders/post",
        "texts","textures/block","textures/colormap","textures/effect","textures/entity",
        "textures/font","textures/gui/sprites","textures/item","textures/map","textures/misc",
        "textures/mob_effect","textures/painting","textures/particle","textures/trims/entity/humanoid",
        "textures/trims/entity/humanoid_leggings","textures/waypoint_style"
    ]
    created = []
    for d in dirs:
        p = os.path.join(base, d)
        if not os.path.isdir(p):
            safe_mkdir(p)
            created.append(p)
    return created
def transform_armor_textures(root, log, ui_progress_step, copy_only=True):
    troot = os.path.join(root, "assets", "minecraft", "textures")
    if not os.path.isdir(troot):
        return 0
    count = 0
    for cur, dirs, files in os.walk(troot):
        for fn in files:
            src = os.path.join(cur, fn)
            lower = fn.lower()
            m1 = ARMOR_LAYER_1_RE.match(fn)
            m2 = ARMOR_LAYER_2_RE.match(fn)
            m3 = LEATHER_OVERLAY_RE.match(fn)
            if m1:
                mat = m1.group("mat")
                dst = os.path.join(troot, "entity", "equipment", "humanoid", f"{mat}.png")
            elif m2:
                mat = m2.group("mat")
                dst = os.path.join(troot, "entity", "equipment", "humanoid_leggings", f"{mat}.png")
            elif m3:
                n = m3.group("n")
                if n == "1":
                    dst = os.path.join(troot, "entity", "equipment", "humanoid", "leather_overlay.png")
                else:
                    dst = os.path.join(troot, "entity", "equipment", "humanoid_leggings", "leather_overlay.png")
            elif lower in ("wolf_armor.png", "wolf_armor_overlay.png"):
                base = "armadillo_scute" if "overlay" not in lower else "armadillo_scute_overlay"
                dst = os.path.join(troot, "entity", "equipment", "wolf_body", f"{base}.png")
            elif lower.startswith("turtle_layer_1"):
                dst = os.path.join(troot, "entity", "equipment", "turtle_scute.png")
            elif "llama" in lower and ("decor" in lower or "decor" in os.path.basename(cur).lower()):
                dst = os.path.join(troot, "entity", "equipment", "llama_body", fn)
            elif "horse" in lower and "armor" in lower:
                dst = os.path.join(troot, "entity", "equipment", "horse_body", fn)
            elif lower == "elytra.png" or fn.lower().endswith("elytra.png"):
                dst = os.path.join(troot, "entity", "equipment", "wings", "elytra.png")
            else:
                continue
            safe_mkdir(os.path.dirname(dst))
            try:
                if copy_only:
                    shutil.copy2(src, dst)
                else:
                    shutil.move(src, dst)
                count += 1
                log.append(f"{now_str()} — Armor moved/copied: {src} -> {dst}")
            except Exception as e:
                log.append(f"{now_str()} — Armor op failed for {src}: {e}")
            ui_progress_step()
    return count
def transform_trims(root, log, ui_progress_step, copy_only=True):
    troot = os.path.join(root, "assets", "minecraft", "textures", "trims", "models", "armor")
    if not os.path.isdir(troot):
        return 0
    count = 0
    for fn in os.listdir(troot):
        src = os.path.join(troot, fn)
        if not os.path.isfile(src):
            continue
        if "_leggings" in fn:
            newname = fn.replace("_leggings", "")
            dst = os.path.join(root, "assets", "minecraft", "textures", "trims", "entity", "humanoid_leggings", newname)
        else:
            dst = os.path.join(root, "assets", "minecraft", "textures", "trims", "entity", "humanoid", fn)
        safe_mkdir(os.path.dirname(dst))
        try:
            if copy_only:
                shutil.copy2(src, dst)
            else:
                shutil.move(src, dst)
            count += 1
            log.append(f"{now_str()} — Trim moved/copied: {src} -> {dst}")
        except Exception as e:
            log.append(f"{now_str()} — Trim op failed for {src}: {e}")
        ui_progress_step()
    return count
def update_model_json_paths(root, log, ui_progress_step):
    count = 0
    models_root = os.path.join(root, "assets", "minecraft", "models")
    if not os.path.isdir(models_root):
        return count
    for dirpath, dirs, files in os.walk(models_root):
        for fn in files:
            if not fn.lower().endswith(".json"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                data = json.load(open(full, "r", encoding="utf-8"))
            except Exception:
                ui_progress_step()
                continue
            modified = False
            def walk_obj(o):
                nonlocal modified, count
                if isinstance(o, dict):
                    for k, v in list(o.items()):
                        if isinstance(v, str):
                            newv = v
                            newv = re.sub(r"(?P<mat>[^/]+?)_layer_1(\.png)?$", r"entity/equipment/humanoid/\g<mat>", newv)
                            newv = re.sub(r"(?P<mat>[^/]+?)_layer_2(\.png)?$", r"entity/equipment/humanoid_leggings/\g<mat>", newv)
                            if newv != v:
                                o[k] = newv
                                modified = True
                                count += 1
                        else:
                            walk_obj(v)
                elif isinstance(o, list):
                    for it in o:
                        walk_obj(it)
            walk_obj(data)
            if modified:
                try:
                    write_json_file(full, data)
                    log.append(f"{now_str()} — Updated model JSON refs in {full}")
                except Exception as e:
                    log.append(f"{now_str()} — Failed to write JSON {full}: {e}")
            ui_progress_step()
    return count
def update_pack_mcmeta(packroot, log, ui_progress_step):
    p = os.path.join(packroot, "pack.mcmeta")
    mc = {}
    if os.path.isfile(p):
        try:
            mc = json.load(open(p, "r", encoding="utf-8"))
        except Exception:
            mc = {}
    if "pack" not in mc:
        mc["pack"] = {}
    mc["pack"]["pack_format"] = 64
    mc["pack"]["description"] = MCMETA_TEXT_COMPONENT
    mc["pack"]["pack_description_legacy"] = "Updated with MewUpdater"
    mc["mew_updater"] = {"updated_by": APP_NAME, "timestamp": now_str()}
    try:
        write_json_file(p, mc)
        log.append(f"{now_str()} — Updated pack.mcmeta (pack_format=64).")
        ui_progress_step()
        return True
    except Exception as e:
        log.append(f"{now_str()} — Failed to write pack.mcmeta: {e}")
        ui_progress_step()
        return False
def write_changelog(root, log_lines):
    changepath = os.path.join(root, "mewupdater_changelog.txt")
    content = f"MewUpdater changelog — {now_str()}\n\n" + "\n".join(log_lines) + "\n"
    write_text_file(changepath, content)
    return changepath
def run_full_update(workdir, ui_log_fn, ui_progress_set, replace_originals=False, mappings=None):
    """
    Runs all transforms and uses UI callbacks to report progress/logs.
    ui_log_fn(msg) -> append msg to log
    ui_progress_set(value) -> set progress 0.0-1.0
    """
    log = []
    def logit(msg):
        log.append(msg); ui_log_fn(msg)
    logit(f"{now_str()} — Starting update in {workdir}")
    created_dirs = ensure_skeleton(workdir)
    for d in created_dirs:
        logit(f"{now_str()} — Created dir: {d}")
    total_steps = 5
    mapping_count = sum(len(v) for v in (mappings.values() if mappings else []))
    total_steps += mapping_count + 30
    step = 0
    def ui_step():
        nonlocal step
        step += 1
        ui_progress_set(min(1.0, step / max(1, total_steps)))
    logit(f"{now_str()} — Processing armor textures...")
    c1 = transform_armor_textures(workdir, log, ui_step, copy_only=not replace_originals)
    logit(f"{now_str()} — Armor/equipment textures processed: {c1}")
    logit(f"{now_str()} — Processing trims...")
    c2 = transform_trims(workdir, log, ui_step, copy_only=not replace_originals)
    logit(f"{now_str()} — Trim textures processed: {c2}")
    if mappings:
        logit(f"{now_str()} — Applying official slicer mappings ({mapping_count} outputs)...")
        c3 = apply_slicer_mappings(workdir, mappings, log, ui_step)
        logit(f"{now_str()} — GUI sprites created: {c3}")
    else:
        logit(f"{now_str()} — No slicer.txt found — skipping official slicer mapping.")
        for _ in range(5):
            ui_step()
    logit(f"{now_str()} — Updating model JSON references...")
    c4 = update_model_json_paths(workdir, log, ui_step)
    logit(f"{now_str()} — Model JSON refs updated: {c4}")
    update_pack_mcmeta(workdir, log, ui_step)
    changepath = write_changelog(workdir, log)
    logit(f"{now_str()} — Wrote changelog: {changepath}")
    ui_progress_set(1.0)
    return log
class MewApp(ctk.CTkFrame):
    def __init__(self, master=None):
        """
        MewApp is now a CTkFrame that can be embedded into a DnD-enabled root (TkinterDnD.Tk).
        If master is None we create a CTk window and pack this frame into it (backwards-compatible).
        """
        super().__init__(master)
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("dark-blue")
        if master is not None:
            try:
                master.title(APP_NAME)
                master.geometry("980x680")
                master.minsize(860, 540)
            except Exception:
                pass
            self.pack(fill="both", expand=True)
        else:
            self._internal_root = ctk.CTk()
            try:
                self._internal_root.title(APP_NAME)
                self._internal_root.geometry("980x680")
                self._internal_root.minsize(860, 540)
            except Exception:
                pass
            self.pack(fill="both", expand=True)
        self.mappings = load_slicer_mappings(SLICER_TXT)
        self._build_ui()
        self.current_theme = "dark"
        self.animating = False
    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=8)
        self.logo_label = ctk.CTkLabel(header, text="")
        self.logo_label.pack(expand=True)
        self._render_logo_large("MewUpdater")
        main = ctk.CTkFrame(self)
        main.pack(fill="both", expand=True, padx=12, pady=8)
        sidebar = ctk.CTkFrame(main, width=220, corner_radius=12)
        sidebar.pack(side="left", fill="y", padx=(0,12), pady=6)
        sidebar.pack_propagate(False)
        ctk.CTkLabel(sidebar, text=APP_NAME, font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(10,8))
        self.buttons = []
        def mkbtn(text, cmd):
            b = ctk.CTkButton(sidebar, text=text, command=cmd, corner_radius=8, fg_color=PINK)
            b.pack(fill="x", padx=12, pady=6)
            b.bind("<Enter>", lambda e, btn=b: btn.configure(fg_color=PINK_HOVER))
            b.bind("<Leave>", lambda e, btn=b: btn.configure(fg_color=PINK))
            self.buttons.append(b)
            return b
        mkbtn("Open Pack", self.open_pack)
        mkbtn("Browse Folder", self.browse_folder)
        mkbtn("Update Pack", self.update_pack)
        mkbtn("Toggle Theme", self.toggle_theme_animated)
        mkbtn("Quit", self.quit)
        ctk.CTkLabel(sidebar, text="Options", anchor="w").pack(fill="x", padx=12, pady=(6,0))
        self.replace_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(sidebar, text="Replace originals", variable=self.replace_var).pack(anchor="w", padx=12, pady=8)
        center = ctk.CTkFrame(main)
        center.pack(side="left", fill="both", expand=True, padx=(0,12), pady=6)
        info = ctk.CTkFrame(center)
        info.pack(fill="x", padx=8, pady=(6,8))
        ctk.CTkLabel(info, text="Selected Pack:").grid(row=0, column=0, sticky="w", padx=6, pady=(6,4))
        self.path_var = ctk.StringVar()
        self.entry = ctk.CTkEntry(info, textvariable=self.path_var, width=520)
        self.entry.grid(row=1, column=0, sticky="we", padx=6)
        browse_btn = ctk.CTkButton(info, text="Browse", command=self.browse_file, width=120, fg_color=PINK)
        browse_btn.grid(row=1, column=1, sticky="e", padx=6)
        browse_btn.bind("<Enter>", lambda e: browse_btn.configure(fg_color=PINK_HOVER))
        browse_btn.bind("<Leave>", lambda e: browse_btn.configure(fg_color=PINK))
        ctk.CTkLabel(info, text="Detected Version:").grid(row=2, column=0, sticky="w", padx=6, pady=(12,4))
        self.detect_var = ctk.StringVar(value="None")
        ctk.CTkLabel(info, textvariable=self.detect_var, font=ctk.CTkFont(size=16, weight="bold")).grid(row=3, column=0, sticky="w", padx=6)
        preview = ctk.CTkFrame(center)
        preview.pack(fill="both", expand=True, padx=8, pady=(0,8))
        ctk.CTkLabel(preview, text="Preview / Log").pack(anchor="w", padx=8, pady=(8,0))
        self.logbox = ctk.CTkTextbox(preview, wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=8, pady=8)
        self.logbox.configure(state="disabled")
        right = ctk.CTkFrame(main, width=260)
        right.pack(side="right", fill="y", pady=6)
        right.pack_propagate(False)
        ctk.CTkLabel(right, text="Actions", font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(8,6))
        ctk.CTkLabel(right, text="Tip: select a zip or folder then Update").pack(pady=(0,8))
        self.progress = ctk.CTkProgressBar(right)
        self.progress.pack(fill="x", padx=12, pady=(12,6))
        self.progress.set(0.0)
        self.preview_count_label = ctk.CTkLabel(right, text="Files to modify: 0")
        self.preview_count_label.pack(pady=(6,4))
        help_btn = ctk.CTkButton(right, text="Show Help", command=self.show_help, fg_color=PINK)
        help_btn.pack(padx=12, pady=8)
        help_btn.bind("<Enter>", lambda e: help_btn.configure(fg_color=PINK_HOVER))
        help_btn.bind("<Leave>", lambda e: help_btn.configure(fg_color=PINK))
    def _render_logo_large(self, text):
        w, h = 900, 150
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            fnt = ImageFont.truetype(ctk.CTkFont(size=48).actual("family") + ".ttf", 48)
        except:
            fnt = ImageFont.load_default()
        bbox = draw.textbbox((0,0), text, font=fnt)
        tw = bbox[2] - bbox[0]; th = bbox[3] - bbox[1]
        x = (w - tw) // 2; y = (h - th) // 2
        mask = Image.new("L", (tw, th), 0)
        md = ImageDraw.Draw(mask)
        md.text((0,0), text, font=fnt, fill=255)
        grad = Image.new("RGBA", (tw, th))
        gd = ImageDraw.Draw(grad)
        for i in range(tw):
            t = i / max(1, tw - 1)
            r = int(GRADIENT_LEFT[0] * (1 - t) + GRADIENT_RIGHT[0] * t)
            g = int(GRADIENT_LEFT[1] * (1 - t) + GRADIENT_RIGHT[1] * t)
            b = int(GRADIENT_LEFT[2] * (1 - t) + GRADIENT_RIGHT[2] * t)
            gd.line([(i,0),(i,th)], fill=(r,g,b,255))
        grad.putalpha(mask)
        img.paste(grad, (x, y), grad)
        self.logo_img = ImageTk.PhotoImage(img)
        self.logo_label.configure(image=self.logo_img)
    def ui_log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
    def set_progress(self, val):
        try:
            self.progress.set(val)
            self.update_idletasks()
        except Exception:
            pass
    def browse_file(self):
        p = filedialog.askopenfilename(title="Select pack .zip", filetypes=[("Zip files","*.zip"),("All files","*.*")])
        if p:
            self.path_var.set(p)
            self.detect_pack(p)
    def browse_folder(self):
        p = filedialog.askdirectory(title="Select pack folder")
        if p:
            self.path_var.set(p)
            self.detect_pack(p)
    def open_pack(self):
        self.browse_file()
    def detect_pack(self, path):
        self.detect_var.set("Detecting...")
        self.update()
        tmp = None
        try:
            if path.lower().endswith(".zip"):
                tmp = tempfile.mkdtemp(prefix="mewdetect_")
                extract_zip_to_dir(path, tmp)
                root = tmp
            else:
                root = path
            mc = read_pack_mcmeta(root)
            pf = None
            if mc and "pack" in mc:
                pf = mc["pack"].get("pack_format")
            self.detect_var.set(f"pack_format {pf}" if pf is not None else "unknown")
            self.ui_log(f"{now_str()} — Detected: pack_format {pf}")
        except Exception as e:
            self.detect_var.set("error")
            self.ui_log(f"{now_str()} — Detection failed: {e}")
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    def toggle_theme_animated(self):
        if self.animating:
            return
        self.animating = True
        steps = 10
        def animate(i):
            if i == steps // 2:
                new = "light" if self.current_theme == "dark" else "dark"
                ctk.set_appearance_mode(new)
                self.current_theme = new
            if i < steps:
                self.after(30, lambda: animate(i+1))
            else:
                self.animating = False
        animate(0)
    def show_help(self):
        txt = (
            "MewUpdater — Help\n\n"
            "1) Select a resource pack .zip or folder.\n"
            "2) Click Update Pack to convert textures to 1.21.7 layout using the official slicer mapping (slicer.txt).\n"
            "3) By default the tool copies files to new locations and leaves originals; check 'Replace originals' to move them.\n\n"
            "The updater will:\n- Move armor/equipment to textures/entity/equipment/*\n- Slice GUI sprites using the official slicer mapping from slicer.txt\n- Update model JSON texture references (best-effort)\n- Update pack.mcmeta to pack_format=64 and set the gradient description\n- Write mewupdater_changelog.txt inside the updated pack\n"
        )
        messagebox.showinfo(APP_NAME + " — Help", txt)
    def update_pack(self):
        path = self.path_var.get().strip()
        if not path:
            messagebox.showwarning(APP_NAME, "No pack selected.")
            return
        self.logbox.configure(state="normal"); self.logbox.delete("1.0", "end"); self.logbox.configure(state="disabled")
        self.set_progress(0.0)
        def _worker():
            tmpdir = tempfile.mkdtemp(prefix="mew_update_")
            try:
                if os.path.isdir(path):
                    workdir = os.path.join(tmpdir, "work")
                    shutil.copytree(path, workdir)
                    original_parent = os.path.dirname(path)
                else:
                    extract_zip_to_dir(path, tmpdir)
                    workdir = tmpdir
                    original_parent = os.path.dirname(path)
                self.ui_log(f"{now_str()} — Work dir: {workdir}")
                ui_log_fn = lambda s: self.after(0, lambda: self.ui_log(s))
                ui_progress_set = lambda v: self.after(0, lambda: self.set_progress(v))
                mappings = self.mappings if self.mappings else None
                run_full_update(workdir, ui_log_fn, ui_progress_set, replace_originals=self.replace_var.get(), mappings=mappings)
                base = os.path.basename(path) if not os.path.isdir(path) else os.path.basename(os.path.abspath(path))
                name = os.path.splitext(base)[0]
                out_zip = os.path.join(original_parent, name + SUFFIX + ".zip")
                i = 1
                final_out = out_zip
                while os.path.exists(final_out):
                    final_out = out_zip.replace(".zip", f"_{i}.zip"); i += 1
                self.ui_log(f"{now_str()} — Zipping updated pack to {final_out} ...")
                create_zip_from_dir(workdir, final_out)
                self.ui_log(f"{now_str()} — Wrote updated pack: {final_out}")
                messagebox.showinfo(APP_NAME, f"Pack updated: {final_out}")
            except Exception as e:
                self.ui_log(f"{now_str()} — ERROR during update: {e}")
                messagebox.showerror(APP_NAME, f"Update failed: {e}")
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
                self.set_progress(0.0)
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
if __name__ == "__main__":
    try:
        try:
            from tkinterdnd2 import TkinterDnD, DND_FILES
            dnd_available = True
            root = TkinterDnD.Tk()
        except Exception:
            import tkinter as tk
            dnd_available = False
            root = tk.Tk()
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("dark-blue")
        app = MewApp(master=root)
        if dnd_available:
            def handle_drop(event):
                raw = event.data
                if not raw:
                    app.ui_log(f"{now_str()} — Drag & drop received empty data.")
                    return
                raw = raw.strip()
                files = []
                temp = ""
                depth = 0
                for ch in raw:
                    if ch == "{":
                        depth += 1
                        if depth == 1:
                            temp = ""
                            continue
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            files.append(temp)
                            continue
                    if depth > 0:
                        temp += ch
                if not files:
                    files = raw.split()
                first_file = files[0] if files else None
                if not first_file:
                    app.ui_log(f"{now_str()} — Failed to parse DnD data: {event.data}")
                    return
                app.ui_log(f"{now_str()} — Dropped file detected: {first_file}")
                app.path_var.set(first_file)
                app.detect_pack(first_file)
            root.drop_target_register(DND_FILES)
            root.dnd_bind("<<Drop>>", handle_drop)
            app.ui_log(f"{now_str()} — Drag & Drop enabled (tkinterdnd2).")
        else:
            app.ui_log(f"{now_str()} — tkinterdnd2 not installed. Run: pip install tkinterdnd2")
        mappings = app.mappings
        if mappings:
            total_outputs = sum(len(v) for v in mappings.values())
            app.preview_count_label.configure(text=f"GUI sprites mapped: {total_outputs}")
            app.ui_log(f"{now_str()} — Loaded official slicer mapping (slicer.txt): {total_outputs} outputs.")
        else:
            app.preview_count_label.configure(text="GUI sprites mapped: 0 (no slicer.txt)")
            app.ui_log(f"{now_str()} — slicer.txt not found; GUI mapping disabled.")
        root.mainloop()
    except ModuleNotFoundError as e:
        missing = str(e).split("'")[1] if "'" in str(e) else str(e)
        print(f"Missing dependency: {missing}")
        print("Install required packages: pip install customtkinter pillow")
        try:
            messagebox.showerror(APP_NAME, f"Missing dependency: {missing}\nInstall with: pip install customtkinter pillow")
        except Exception:
            pass
        sys.exit(1)