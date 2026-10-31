"""Lecteur secret : une vidéo cachée que seules des lunettes synchronisées révèlent.

Principe (modulation psychovisuelle temporelle) : l'écran alterne deux trames à chaque
rafraîchissement. En lumière linéaire, A = leurre + d et B = leurre - d. L'œil nu fait la
moyenne des deux et ne voit que le leurre. Des lunettes ouvertes seulement pendant A
voient leurre + d, c'est-à-dire la vidéo.

Lancement : glisser une vidéo sur LANCER.bat, ou
    python lecteur_secret.py ma_video.mp4 [--leurre ecran|gris|image.png] [--fenetre]
"""
import argparse
import collections
import ctypes
import sys
import threading
import time

import cv2
import glfw
import moderngl
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageOps

TITRE = "Lecteur secret"
BANDES_ENVOI = 4   # une image vidéo est envoyée à la carte graphique en 4 morceaux, sur 4 images écran
QUAD = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4")

VERT = """
#version 330
in vec2 in_pos;
out vec2 uv;
void main() { uv = in_pos * 0.5 + 0.5; gl_Position = vec4(in_pos, 0.0, 1.0); }
"""

# Tout le calcul se fait en lumière linéaire : l'œil moyenne des photons, pas des valeurs de pixel.
FRAG = """
#version 330
uniform sampler2D u_video;
uniform sampler2D u_decoy;
uniform int   u_has_video;
uniform int   u_decoy_mode;   // 0 = gris uni, 1 = image (capture d'écran ou fichier)
uniform float u_gray;         // lumière linéaire du gris uni
uniform float u_lift;         // éclaircit le leurre pour gagner de la marge
uniform float u_gain;         // intensité du secret, 0..1
uniform int   u_phase;        // 0 = trame A (secret), 1 = trame B (compensation)
uniform vec4  u_rect;         // zone du secret en uv : x0, y0, x1, y1
uniform vec4  u_patch;        // barre de synchro pour le capteur des lunettes
uniform int   u_patch_on;
uniform int   u_patch_cache;  // 1 = barre invisible à l'œil nu, 0 = barre blanc/noir (signal maximal)
uniform float u_gamma;        // courbe de l'écran (≈ 2.2), réglable pour effacer un fantôme fixe
uniform vec3  u_mean;         // écart moyen secret/leurre dans la zone, retiré pour ne pas brouiller le capteur
in vec2 uv;
out vec4 frag;

vec3 lin(vec3 c)  { return pow(c, vec3(u_gamma)); }
vec3 srgb(vec3 l) { return pow(clamp(l, 0.0, 1.0), vec3(1.0 / u_gamma)); }

void main() {
    vec3 dL = (u_decoy_mode == 1) ? lin(texture(u_decoy, vec2(uv.x, 1.0 - uv.y)).rgb) : vec3(u_gray);
    dL = mix(dL, vec3(0.5), u_lift);

    if (u_patch_on == 1 && all(greaterThanEqual(uv, u_patch.xy)) && all(lessThanEqual(uv, u_patch.zw))) {
        if (u_patch_cache == 1) {
            // Barre invisible : même principe que le secret, avec tout l'écart disponible. L'œil fait la
            // moyenne et ne voit que le leurre ; le capteur voit toujours A plus clair que B.
            vec3 m = min(dL, 1.0 - dL);
            frag = vec4(srgb(u_phase == 0 ? dL + m : dL - m), 1.0);
        } else {
            frag = vec4(vec3(u_phase == 0 ? 1.0 : 0.0), 1.0);
        }
        return;
    }

    vec3 ecart = vec3(0.0);   // hors de la zone secrète : A = B = leurre
    if (u_has_video == 1 && all(greaterThanEqual(uv, u_rect.xy)) && all(lessThanEqual(uv, u_rect.zw))) {
        vec2 t = (uv - u_rect.xy) / (u_rect.zw - u_rect.xy);
        // Sans u_mean, une scène sombre rendrait toute la zone plus sombre en A qu'en B, et un capteur
        // qui regarde l'écran se tromperait de trame. Avec, la zone ne pèse plus rien : seule la barre compte.
        ecart = lin(texture(u_video, vec2(t.x, 1.0 - t.y)).bgr) - dL - u_mean;   // OpenCV fournit du BGR
    }

    // A et B doivent rester dans [0, 1], sinon la moyenne ne retombe plus sur le leurre.
    // On borne donc l'écart à la marge disponible : aucune fuite possible, quel que soit le réglage.
    vec3 m = min(dL, 1.0 - dL);
    vec3 d = clamp(ecart * u_gain, -m, m);
    frag = vec4(srgb(u_phase == 0 ? dL + d : dL - d), 1.0);
}
"""

HUD_VERT = """
#version 330
uniform vec4 u_rect;   // coins en coordonnées écran normalisées
in vec2 in_pos;
out vec2 uv;
void main() { uv = in_pos * 0.5 + 0.5; gl_Position = vec4(mix(u_rect.xy, u_rect.zw, uv), 0.0, 1.0); }
"""

HUD_FRAG = """
#version 330
uniform sampler2D u_tex;
in vec2 uv;
out vec4 frag;
void main() { frag = texture(u_tex, vec2(uv.x, 1.0 - uv.y)); }
"""


def build_programs(ctx):
    main = ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
    hud = ctx.program(vertex_shader=HUD_VERT, fragment_shader=HUD_FRAG)
    vbo = ctx.buffer(QUAD.tobytes())
    return (main, hud,
            ctx.vertex_array(main, [(vbo, "2f", "in_pos")]),
            ctx.vertex_array(hud, [(vbo, "2f", "in_pos")]))


def set_uniform(prog, name, value):
    u = prog.get(name, None)   # un uniform inutilisé est retiré par le compilateur
    if u is not None:
        u.value = value


HAUTEUR_SECRET = 0.46   # au-delà, la zone n'est plus entièrement en trame A assez longtemps (balayage)


def secret_rect(W, H, vw, vh, layout):
    """Zone du secret en uv (origine en bas à gauche), proportions de la vidéo conservées.

    En mode lunettes (coin ou centre), c'est la hauteur qui est limitée, pas la largeur : l'écran se
    redessine de haut en bas, et une zone haute n'est jamais entièrement en trame A bien longtemps.
    Une vidéo sans bandes noires (format cinéma) peut donc s'étaler en largeur.
    """
    if vw <= 0 or vh <= 0:
        return (0.0, 0.0, 1.0, 1.0)
    if layout in ("coin", "centre"):
        marge = 0.02 * min(W, H)
        rh = HAUTEUR_SECRET * H
        rw = rh * vw / vh
        if rw > W - 2 * marge:
            rw = W - 2 * marge
            rh = rw * vh / vw
        if layout == "coin":
            x0, y0 = W - marge - rw, marge
        else:   # centré, en laissant la place de la barre de synchro au-dessus
            x0, y0 = (W - rw) / 2, (H - rh - max(6, 0.02 * H)) / 2
    else:
        s = min(W / vw, H / vh)
        rw, rh = vw * s, vh * s
        x0, y0 = (W - rw) / 2, (H - rh) / 2
    return (x0 / W, y0 / H, (x0 + rw) / W, (y0 + rh) / H)


def barre_rect(W, H, rect):
    """Barre de synchro sur toute la largeur de la zone secrète, collée à son bord haut.

    L'écran se rafraîchit ligne par ligne, de haut en bas : la barre change de trame juste avant
    les premières lignes du secret. Sur toute la largeur, elle donne beaucoup plus de signal
    qu'un petit carré à un capteur placé sur les lunettes.
    """
    bh = max(6, 0.02 * H) / H
    x0, x1, y1 = rect[0], rect[2], rect[3]
    if y1 + bh <= 1.0:
        return (x0, y1, x1, y1 + bh)   # juste au-dessus de la zone
    return (x0, y1 - bh, x1, y1)       # zone collée en haut de l'écran : on prend ses premières lignes


def moyenne_leurre(decoy_np, rect, lift, gamma):
    """Lumière moyenne (linéaire) du leurre dans la zone secrète. decoy_np = None pour le gris uni."""
    if decoy_np is None:
        return np.full(3, 0.5, np.float32)
    H, W = decoy_np.shape[:2]
    x0 = int(rect[0] * W)
    y0 = int((1 - rect[3]) * H)   # rect a son origine en bas, l'image en haut
    x1 = max(x0 + 1, int(rect[2] * W))
    y1 = max(y0 + 1, int((1 - rect[1]) * H))
    petit = cv2.resize(decoy_np[y0:y1, x0:x1], (64, 36), interpolation=cv2.INTER_AREA)
    lin = (petit.astype(np.float32) / 255.0) ** gamma
    return (lin * (1 - lift) + 0.5 * lift).reshape(-1, 3).mean(axis=0)


def moyenne_video(petit_bgr, gamma):
    """Lumière moyenne (linéaire, RVB) d'une image de la vidéo réduite."""
    return ((petit_bgr[..., ::-1].astype(np.float32) / 255.0) ** gamma).reshape(-1, 3).mean(axis=0)


def bandes_noires(cap, n=24):
    """Lignes utiles (début, fin) d'une vidéo au format cinéma, sans les bandes noires du haut et du bas.

    On regarde n images réparties dans la vidéo : une ligne fait partie d'une bande si elle est noire
    sur la plupart d'entre elles (les cartons de titre, eux, peuvent déborder dans les bandes).
    """
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    allumees = []
    for k in range(n):
        if total > n:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int((k + 0.5) * total / n))
        ok, f = cap.read()
        if ok:
            allumees.append(f.max(axis=(1, 2)) > 25)
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    if len(allumees) < 4:
        return None
    part = np.mean(allumees, axis=0)
    utiles = np.where(part > 0.5 * part.max())[0]
    h = len(part)
    y0, y1 = int(utiles[0]) & ~1, min(h, (int(utiles[-1]) + 2) & ~1)
    if (h - (y1 - y0)) < 0.04 * h:   # moins de 4 % de bandes : on ne touche à rien
        return None
    return y0, y1


class VideoReader(threading.Thread):
    """Décode la vidéo au bon rythme dans un fil séparé, pour ne jamais bloquer l'affichage.

    Tout le travail sur l'image (recadrage, réduction, passage en BGRA) se fait ici : le fil
    d'affichage n'a plus qu'à l'envoyer à la carte graphique.
    """

    def __init__(self, path, max_w=1920, recadrer=True):
        super().__init__(daemon=True)
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise ValueError(f"Impossible d'ouvrir la vidéo : {path}")
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if 1 < fps <= 240 else 30.0
        self.max_w = max_w
        self.lignes = bandes_noires(self.cap) if recadrer else None
        if self.lignes:
            print(f"Bandes noires retirées : on garde les lignes {self.lignes[0]} à {self.lignes[1]}", flush=True)
        self.lock = threading.Lock()
        self.frame, self.petit, self.seq = None, None, 0
        self.paused = False
        self._stop = threading.Event()

    def latest(self):
        with self.lock:
            return self.seq, self.frame, self.petit

    def stop(self):
        self._stop.set()

    def run(self):
        period = 1.0 / self.fps
        next_t = time.perf_counter()
        echecs = 0
        while not self._stop.is_set():
            if self.paused:
                time.sleep(0.02)
                next_t = time.perf_counter()
                continue
            ok, f = self.cap.read()
            if not ok:
                echecs += 1
                if echecs > 2:   # fichier illisible : on arrête plutôt que de boucler à vide
                    return
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)   # fin de la vidéo : on reboucle
                continue
            echecs = 0
            if self.lignes:
                f = f[self.lignes[0]:self.lignes[1]]
            if f.shape[1] > self.max_w:
                h = int(f.shape[0] * self.max_w / f.shape[1])
                f = cv2.resize(f, (self.max_w, h), interpolation=cv2.INTER_AREA)
            petit = cv2.resize(f, (64, 36), interpolation=cv2.INTER_AREA)   # pour l'équilibrage
            f = cv2.cvtColor(f, cv2.COLOR_BGR2BGRA)   # 4 octets par pixel : envoi direct, sans conversion
            with self.lock:
                self.frame, self.petit, self.seq = f, petit, self.seq + 1
            next_t += period
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:
                next_t = time.perf_counter()


def load_font(size):
    for name in ("consola.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


class Hud:
    """Texte dessiné par-dessus (identique dans A et B, donc visible par tous)."""

    def __init__(self, ctx, prog, vao, size=20):
        self.ctx, self.prog, self.vao = ctx, prog, vao
        self.size = size
        self.font = load_font(size)
        self.tex = None

    def update(self, lines, center=False):
        pad, lh = int(self.size * 0.7), int(self.size * 1.4)
        mesure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        largeurs = [mesure.textlength(l, font=self.font) for l in lines]
        w = int(max(largeurs)) + 2 * pad
        h = lh * len(lines) + 2 * pad
        img = Image.new("RGBA", (w, h), (10, 12, 16, 200))
        draw = ImageDraw.Draw(img)
        for i, (l, lw) in enumerate(zip(lines, largeurs)):
            x = (w - lw) / 2 if center else pad
            draw.text((x, pad + i * lh), l, font=self.font, fill=(232, 237, 244, 255))
        if self.tex is None or self.tex.size != img.size:
            if self.tex is not None:
                self.tex.release()
            self.tex = self.ctx.texture(img.size, 4, img.tobytes())
        else:
            self.tex.write(img.tobytes())

    def draw(self, W, H, where="haut"):
        if self.tex is None:
            return
        w, h = self.tex.size
        if where == "bas":   # centré en bas, pour les légendes de la démo
            x0, y0 = -w / W, -1 + 0.12
            rect = (x0, y0, x0 + 2 * w / W, y0 + 2 * h / H)
        else:
            m = 16
            x0, y1 = -1 + 2 * m / W, 1 - 2 * m / H
            rect = (x0, y1 - 2 * h / H, x0 + 2 * w / W, y1)
        set_uniform(self.prog, "u_rect", rect)
        set_uniform(self.prog, "u_tex", 2)
        self.tex.use(2)
        self.ctx.enable(moderngl.BLEND)
        self.vao.render(moderngl.TRIANGLE_STRIP)
        self.ctx.disable(moderngl.BLEND)


DEMO_FREQS = (1, 2, 4, 8, 15, 30, 60)   # alternances par seconde, avant la vitesse réelle


def demo_plan(refresh):
    """Étapes de la démo : (durée en s, maintien en images ou None, trame fixe, légende)."""
    etapes = [(4.0, None, 0, "1. La vidéo secrète"),
              (4.0, None, 1, "2. Son négatif, calculé pour l'annuler")]
    for f in DEMO_FREQS:
        hold = max(1, round(refresh / (2 * f)))
        if hold == 1:
            break
        etapes.append((4.0 if f <= 2 else 3.0, hold, 0,
                       f"3. L'écran alterne les deux : {refresh / (2 * hold):.0f} fois par seconde"))
    etapes.append((6.0, 1, 0, f"À {refresh / 2:.0f} fois par seconde, ton œil fait la moyenne : la vidéo a disparu"))
    return etapes


def demo_step(plan, elapsed):
    for etape in plan:
        if elapsed < etape[0]:
            return etape
        elapsed -= etape[0]
    return None


def pick_file():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="Choisis la vidéo secrète",
        filetypes=[("Vidéos", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("Tous les fichiers", "*.*")])
    root.destroy()
    return path or None


def best_mode(monitor):
    """Mode à la résolution actuelle avec le rafraîchissement le plus élevé (240 Hz attendu)."""
    cur = glfw.get_video_mode(monitor)
    same = [m for m in glfw.get_video_modes(monitor)
            if m.size.width == cur.size.width and m.size.height == cur.size.height]
    return max(same, key=lambda m: m.refresh_rate) if same else cur


def parse_args(argv):
    p = argparse.ArgumentParser(description="Lecteur de vidéo secrète (moniteur secret).")
    p.add_argument("video", nargs="?", help="fichier vidéo (sinon une fenêtre s'ouvre pour le choisir)")
    p.add_argument("--leurre", default="ecran",
                   help="ecran (capture de ton écran, par défaut), gris, ou chemin d'une image")
    p.add_argument("--intensite", type=float, default=0.6, help="intensité du secret, 0.05 à 1")
    p.add_argument("--gamma", type=float, default=2.2, help="courbe de l'écran, 1.8 à 2.6")
    p.add_argument("--largeur-max", type=int, default=1920,
                   help="largeur max de la vidéo décodée (1280 si des images sautent)")
    p.add_argument("--disposition", choices=("plein", "coin", "centre"), default="plein",
                   help="secret en plein écran, dans un coin ou au centre (coin ou centre avec les lunettes)")
    p.add_argument("--maintien", type=int, choices=(1, 2), default=1,
                   help="images affichées par trame : 2 = A A B B, pour des lunettes avec un secret plein écran")
    p.add_argument("--compenser", action="store_true",
                   help="expérimental : rééquilibre A et B après une image sautée")
    p.add_argument("--demo", action="store_true",
                   help="lance la démo toute seule 5 s après l'ouverture, panneau masqué (pour filmer)")
    p.add_argument("--demo-touche", action="store_true",
                   help="pour filmer la démo : écran normal sans panneau, la démo part quand on appuie sur M")
    p.add_argument("--lunettes", action="store_true",
                   help="mode lunettes : secret au centre (ou --disposition coin), luminosité équilibrée pour le capteur")
    p.add_argument("--vue-lunettes", action="store_true",
                   help="affiche seulement la trame A, ce que voient les lunettes (touche V), pour filmer")
    p.add_argument("--sans-barre", action="store_true",
                   help="pas de barre de synchro (touche P) : les lunettes ne peuvent plus se caler")
    p.add_argument("--barre-visible", action="store_true",
                   help="barre de synchro blanc/noir (signal maximal, mais visible) au lieu d'invisible")
    p.add_argument("--garder-bandes", action="store_true",
                   help="garde les bandes noires des vidéos au format cinéma (retirées par défaut)")
    p.add_argument("--fenetre", action="store_true", help="fenêtre au lieu du plein écran (pour essayer)")
    return p.parse_args(argv)


def run(args, reader, decoy_img):
    monitor = glfw.get_primary_monitor()
    mode = best_mode(monitor)
    refresh = mode.refresh_rate or 60

    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
    glfw.window_hint(glfw.REFRESH_RATE, refresh)
    # Reste affiché quand on clique sur un autre écran (moniteur série des lunettes, par exemple).
    glfw.window_hint(glfw.AUTO_ICONIFY, False)
    if args.fenetre:
        win = glfw.create_window(1280, 720, TITRE, None, None)
    else:
        win = glfw.create_window(mode.size.width, mode.size.height, TITRE, monitor, None)
    if not win:
        raise SystemExit("Impossible d'ouvrir la fenêtre OpenGL (pilote graphique ?).")
    glfw.make_context_current(win)
    glfw.swap_interval(1)   # une image par rafraîchissement, calée sur l'écran
    if not args.fenetre:
        glfw.set_input_mode(win, glfw.CURSOR, glfw.CURSOR_HIDDEN)

    ctx = moderngl.create_context()
    main, hud_prog, vao, hud_vao = build_programs(ctx)
    hud = Hud(ctx, hud_prog, hud_vao)
    legende_hud = Hud(ctx, hud_prog, hud_vao, size=40)
    plan = demo_plan(refresh)

    W, H = glfw.get_framebuffer_size(win)
    decoy_tex = decoy_np = None
    if decoy_img is not None:
        img = ImageOps.fit(decoy_img.convert("RGB"), (W, H))
        decoy_tex = ctx.texture((W, H), 3, img.tobytes())
        decoy_np = np.asarray(img)
    dummy = ctx.texture((1, 1), 3, bytes(3))
    vtex, vtex_suiv, envoi, pret, last_seq = None, None, None, None, -1

    st = {
        "gain": min(1.0, max(0.0, args.intensite)),
        "lift": 0.3 if decoy_tex is not None else 0.0,
        "layout": ("centre" if args.disposition == "plein" else args.disposition) if args.lunettes else args.disposition,
        "decoy": 1 if decoy_tex is not None else 0,
        "equil": args.lunettes,
        # Le panneau coûte une image sautée à chaque mise à jour : masqué d'office avec les lunettes (H pour l'afficher).
        "patch": not args.sans_barre, "hud": not (args.demo or args.demo_touche or args.lunettes), "slow": False,
        "legendes": True,
        "cache": not args.barre_visible,
        "vue": args.vue_lunettes,
        "gamma": min(2.6, max(1.8, args.gamma)),
        "demo": time.perf_counter() + 5.0 if args.demo else None,   # instant de départ de la démo
    }

    def on_key(_w, key, _sc, action, mods):
        if action not in (glfw.PRESS, glfw.REPEAT):
            return
        if key in (glfw.KEY_ESCAPE, glfw.KEY_Q):
            glfw.set_window_should_close(win, True)
        elif key == glfw.KEY_SPACE:
            reader.paused = not reader.paused
        elif key == glfw.KEY_UP:
            st["gain"] = min(1.0, round(st["gain"] + 0.05, 2))
        elif key == glfw.KEY_DOWN:
            st["gain"] = max(0.0, round(st["gain"] - 0.05, 2))   # 0 : secret coupé, l'écran n'affiche que le leurre
        elif key == glfw.KEY_RIGHT:
            st["lift"] = min(0.8, round(st["lift"] + 0.05, 2))
        elif key == glfw.KEY_LEFT:
            st["lift"] = max(0.0, round(st["lift"] - 0.05, 2))
        elif key == glfw.KEY_C:
            st["layout"] = {"plein": "coin", "coin": "centre", "centre": "plein"}[st["layout"]]
        elif key == glfw.KEY_D and decoy_tex is not None:
            st["decoy"] ^= 1
        elif key == glfw.KEY_P:
            st["patch"] = not st["patch"]
        elif key == glfw.KEY_I:
            st["cache"] = not st["cache"]
        elif key == glfw.KEY_H:
            st["hud"] = not st["hud"]
        elif key == glfw.KEY_B:
            st["slow"] = not st["slow"]
        elif key == glfw.KEY_V:
            st["vue"] = not st["vue"]
        elif key == glfw.KEY_E:
            st["equil"] = not st["equil"]
        elif key == glfw.KEY_M:   # démo pédagogique, relancée à chaque appui
            st["demo"] = time.perf_counter()
            st["hud"] = False
        elif key == glfw.KEY_N and st["demo"] is not None:   # démo : étape suivante tout de suite
            ecoule, fin = time.perf_counter() - st["demo"], 0.0
            for duree, *_ in plan:
                fin += duree
                if ecoule < fin:
                    st["demo"] -= fin - ecoule
                    break
        elif key == glfw.KEY_T:   # légendes de la démo
            st["legendes"] = not st["legendes"]
        elif key == glfw.KEY_G:
            pas = -0.1 if mods & glfw.MOD_SHIFT else 0.1
            st["gamma"] = min(2.6, max(1.8, round(st["gamma"] + pas, 1)))

    glfw.set_key_callback(win, on_key)

    frames, phase = 0, 0
    fps = float(refresh)
    drops, recent = 0, collections.deque()
    t_start = last = time.perf_counter()
    next_hud = next_log = 0.0
    derniere_legende = None
    decalage = 0   # rééquilibrage A/B après une image sautée (--compenser)
    video_moy = None                          # lumière moyenne de l'image vidéo en cours
    cle_leurre, leurre_moy = None, None       # lumière moyenne du leurre dans la zone, en cache

    while not glfw.window_should_close(win):
        glfw.poll_events()
        W, H = glfw.get_framebuffer_size(win)
        if W == 0 or H == 0:   # fenêtre réduite
            time.sleep(0.05)
            last = time.perf_counter()
            continue

        # Une image vidéo entière (jusqu'à 8 Mo) envoyée d'un coup peut dépasser les 4 ms d'une image
        # à 240 Hz, et l'écran saute alors une trame. On l'envoie donc par bandes, sur plusieurs tours,
        # dans une seconde texture qu'on échange avec la première une fois complète.
        if envoi is None and pret is None:
            seq, frame, petit = reader.latest()
            if frame is not None and seq != last_seq:
                last_seq = seq
                h, w = frame.shape[:2]
                if vtex is None or vtex.size != (w, h):   # première image, ou taille qui change
                    for t in (vtex, vtex_suiv):
                        if t is not None:
                            t.release()
                    vtex, vtex_suiv = ctx.texture((w, h), 4, frame), ctx.texture((w, h), 4, frame)
                    for t in (vtex, vtex_suiv):
                        t.repeat_x = t.repeat_y = False
                    video_moy = moyenne_video(petit, st["gamma"])
                else:
                    envoi = [frame, petit, 0]
        if envoi is not None:
            frame_e, petit_e, y = envoi
            h, w = frame_e.shape[:2]
            n = min(-(-h // BANDES_ENVOI), h - y)
            vtex_suiv.write(frame_e[y:y + n], viewport=(0, y, w, n))
            envoi[2] = y + n
            if envoi[2] >= h:
                pret, envoi = petit_e, None   # échangée juste avant une trame A (plus bas)

        etape = None
        if st["demo"] is not None:
            ecoule = time.perf_counter() - st["demo"]
            if ecoule >= 0:   # avant le départ : affichage normal
                etape = demo_step(plan, ecoule)
                if etape is None:
                    st["demo"] = None   # démo terminée
        if etape is None:
            legende = None
            phase = (frames // 30) % 2 if st["slow"] else ((frames + decalage) // args.maintien) % 2
            if st["vue"]:   # ce que voient les lunettes : la trame A seule, en continu (pour filmer)
                phase = 0
        else:
            _, hold, fixe, legende = etape
            phase = fixe if hold is None else (frames // hold) % 2
            if not st["legendes"]:
                legende = None
        # Nouvelle image vidéo seulement au début d'une paire A/B : A et B montrent alors la même image,
        # et leur moyenne retombe exactement sur le leurre, même sur ce qui bouge.
        if pret is not None and (phase == 0 or etape is not None):   # la démo peut figer la trame B
            vtex, vtex_suiv = vtex_suiv, vtex
            video_moy = moyenne_video(pret, st["gamma"])
            pret = None
        if legende != derniere_legende:   # le rendu du texte coûte : seulement quand il change
            if legende:
                legende_hud.update([legende], center=True)
            derniere_legende = legende

        ctx.viewport = (0, 0, W, H)
        (vtex or dummy).use(0)
        (decoy_tex or dummy).use(1)
        set_uniform(main, "u_video", 0)
        set_uniform(main, "u_decoy", 1)
        set_uniform(main, "u_has_video", 1 if vtex is not None else 0)
        set_uniform(main, "u_decoy_mode", st["decoy"])
        set_uniform(main, "u_gray", 0.5)   # gris uni = moitié de la lumière max (≈ 186 en sRGB)
        set_uniform(main, "u_lift", st["lift"] if st["decoy"] == 1 else 0.0)
        set_uniform(main, "u_gain", st["gain"])
        set_uniform(main, "u_phase", phase)
        vw, vh = vtex.size if vtex is not None else (16, 9)
        rect = secret_rect(W, H, vw, vh, st["layout"])
        set_uniform(main, "u_rect", rect)
        set_uniform(main, "u_patch", barre_rect(W, H, rect))
        set_uniform(main, "u_patch_on", 1 if st["patch"] else 0)
        set_uniform(main, "u_patch_cache", 1 if st["cache"] else 0)
        set_uniform(main, "u_gamma", st["gamma"])
        moy = (0.0, 0.0, 0.0)
        if st["equil"] and video_moy is not None:
            lift = st["lift"] if st["decoy"] == 1 else 0.0
            cle = (rect, st["decoy"], lift, st["gamma"])
            if cle != cle_leurre:   # ne change qu'avec les réglages, pas à chaque image
                cle_leurre = cle
                leurre_moy = moyenne_leurre(decoy_np if st["decoy"] == 1 else None, rect, lift, st["gamma"])
            moy = tuple(float(x) for x in video_moy - leurre_moy)
        set_uniform(main, "u_mean", moy)
        vao.render(moderngl.TRIANGLE_STRIP)
        if legende:
            legende_hud.draw(W, H, "bas")
        if st["hud"]:
            hud.draw(W, H)

        glfw.swap_buffers(win)
        ctx.finish()   # attend l'affichage réel : le chrono mesure alors les vraies images

        now = time.perf_counter()
        dt, last = now - last, now
        if dt > 0:
            fps += (1.0 / dt - fps) * 0.05
        if now - t_start > 2.0 and dt > 1.5 / refresh:   # on ignore le démarrage
            n = max(1, round(dt * refresh) - 1)
            drops += n
            recent.extend([now] * n)
            print(f"  image sautée à {now - t_start:.1f} s ({n})", flush=True)
            # La trame précédente est restée affichée deux fois : on répète celle-ci une fois pour
            # rééquilibrer. Fenêtre étroite, car un faux positif créerait lui-même un flash.
            if (args.compenser and args.maintien == 1 and not st["slow"] and st["demo"] is None
                    and 1.75 / refresh < dt < 2.5 / refresh):
                decalage += 1
        while recent and now - recent[0] > 5.0:
            recent.popleft()
        frames += 1
        if now >= next_log:   # journal dans la console, pour vérifier sans regarder l'écran
            next_log = now + 2.0
            print(f"[{now - t_start:6.1f} s] {fps:5.1f} img/s  sautées {drops} (5 s : {len(recent)})  "
                  f"intensité {st['gain']:.2f}  gamma {st['gamma']:.1f}  "
                  f"leurre {'écran' if st['decoy'] == 1 else 'gris'}  panneau {'oui' if st['hud'] else 'non'}"
                  f"{'  DÉMO' if st['demo'] is not None else ''}",
                  flush=True)

        if st["hud"] and now >= next_hud:
            next_hud = now + 1.0   # le dessin du texte coûte quelques ms : pas trop souvent
            avert = "   ATTENTION : il faut au moins 120 Hz" if refresh < 120 else ""
            hud.update([
                f"LECTEUR SECRET   écran {refresh} Hz   affiché {fps:.0f} img/s   "
                f"images sautées : {drops} (5 dernières s : {len(recent)}){avert}",
                f"Intensité du secret {st['gain'] * 100:.0f} %  [Haut/Bas]      "
                f"Éclaircir le leurre {st['lift'] * 100:.0f} %  [Gauche/Droite]      "
                f"Gamma {st['gamma']:.1f}  [G / Maj+G]",
                f"Leurre : {'ton écran' if st['decoy'] == 1 else 'gris uni'}  [D]      "
                f"Secret : {st['layout']}  [C]      Barre de synchro : {'oui' if st['patch'] else 'non'}  [P]"
                f" {'invisible' if st['cache'] else 'blanc/noir'}  [I]      "
                f"Alternance : {'LENTE (debug)' if st['slow'] else 'réelle'}  [B]"
                f"{'   VUE LUNETTES' if st['vue'] else ''}  [V]      "
                f"Maintien : {args.maintien} image(s) par trame      "
                f"Équilibrage lunettes : {'oui' if st['equil'] else 'non'}  [E]",
                f"Vidéo {reader.fps:.0f} img/s{'  EN PAUSE' if reader.paused else ''}      "
                "M démo (N étape suivante, T légendes)      Espace pause      H masquer ce panneau      Échap quitter",
            ])

    duree = time.perf_counter() - t_start
    print(f"FIN : {duree:.0f} s, {frames} images, {frames / max(duree, 1e-6):.1f} img/s en moyenne, "
          f"{drops} images sautées, gamma final {st['gamma']:.1f}, intensité finale {st['gain']:.2f}", flush=True)
    glfw.destroy_window(win)
    return 0


def console_window():
    try:
        return ctypes.windll.kernel32.GetConsoleWindow()
    except (AttributeError, OSError):
        return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # vraies dimensions en pixels, même avec la mise à l'échelle Windows
        # Priorité haute et minuterie Windows à 1 ms : moins de risque qu'une autre appli fasse sauter une image.
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x80)   # HIGH_PRIORITY_CLASS
        ctypes.windll.winmm.timeBeginPeriod(1)
    except (AttributeError, OSError):
        pass

    path = args.video or pick_file()
    if not path:
        print("Aucune vidéo choisie.")
        return 1
    try:
        reader = VideoReader(path, max_w=args.largeur_max, recadrer=not args.garder_bandes)
    except ValueError as e:
        print(e)
        return 1

    decoy_img = None
    console = console_window()
    if args.leurre == "ecran":
        if console:
            ctypes.windll.user32.ShowWindow(console, 6)   # réduit la console pour qu'elle n'apparaisse pas dans le leurre
            time.sleep(0.6)
        decoy_img = ImageGrab.grab()
    elif args.leurre != "gris":
        try:
            decoy_img = Image.open(args.leurre)
        except OSError:
            print(f"Image de leurre illisible : {args.leurre}. On passe au gris uni.")

    if not glfw.init():
        print("GLFW n'a pas pu démarrer.")
        return 1
    reader.start()
    try:
        return run(args, reader, decoy_img)
    finally:
        reader.stop()
        glfw.terminate()
        if console:
            ctypes.windll.user32.ShowWindow(console, 9)


if __name__ == "__main__":
    sys.exit(main())
