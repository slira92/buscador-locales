"""
Buscador de ubicaciones comerciales (strip centers, stand alone, placas) - Portal Inmobiliario.

Para cada ciudad y operacion (arriendo / venta) de config.json recorre la categoria "comercial"
con un rango de superficie ampliado, entra a cada ficha, clasifica tipo de local, calce de m2,
quien publica, y calcula UF/m2, comparacion con el mercado de la ciudad y un puntaje 0-100.
Guarda data/avisos.json, genera dashboard.html y avisa de lo nuevo que calza.

Uso:
    py buscador_locales.py                  ejecucion normal
    py buscador_locales.py --solo-dashboard recalcula y regenera dashboard.html sin scrapear
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
LOGS = BASE / "logs"
AVISOS_JSON = DATA / "avisos.json"
LOCK = DATA / ".lock"
DASHBOARD = BASE / "dashboard.html"
PLANTILLA = BASE / "plantilla_dashboard.html"

URL_BUSQUEDA = "https://www.portalinmobiliario.com/{op}/{tipo}/{slug}/_{campo}*AREA_{a}m²-{b}m²"
URL_FOTO = "https://http2.mlstatic.com/D_NQ_NP_{id}-O.webp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9",
}


# ---------------------------------------------------------------- utilidades (mismas del buscador de deptos)

def log(msg):
    LOGS.mkdir(exist_ok=True)
    linea = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(linea, flush=True)
    with open(LOGS / f"locales_{datetime.now():%Y-%m}.log", "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def ahora():
    return datetime.now().isoformat(timespec="seconds")


class Bloqueado(Exception):
    pass


class Cliente:
    def __init__(self, pausa):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.pausa = pausa
        self.ultimo = 0.0

    def get(self, url, intentos=3):
        espera = random.uniform(*self.pausa) - (time.time() - self.ultimo)
        if espera > 0:
            time.sleep(espera)
        for i in range(intentos):
            try:
                r = self.s.get(url, timeout=30)
                self.ultimo = time.time()
                if r.status_code == 200:
                    return r.text
                if r.status_code == 404:
                    return None  # el portal responde 404 cuando un filtro no tiene resultados
                if r.status_code in (403, 429):
                    raise Bloqueado(f"HTTP {r.status_code} en {url}")
                log(f"  HTTP {r.status_code} en {url} (intento {i + 1})")
            except requests.RequestException as e:
                log(f"  error de red {e.__class__.__name__} (intento {i + 1})")
            time.sleep(5 * (i + 1))
        return None


def estado_nordic(html):
    m = re.search(r'id="__NORDIC_RENDERING_CTX__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise Bloqueado("la pagina no trae __NORDIC_RENDERING_CTX__ (posible captcha)")
    body = m.group(1)
    d, _ = json.JSONDecoder().raw_decode(body[body.find("{"):])
    pp = (d.get("appProps") or {}).get("pageProps") or {}
    if "initialState" not in pp:
        raise Bloqueado("pagina inesperada (verificacion de cuenta)")
    return pp["initialState"]


def recorrer(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from recorrer(v)
    elif isinstance(o, list):
        for v in o:
            yield from recorrer(v)


def numeros(texto):
    return [float(x.replace(".", "").replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)*", texto or "")]


def monto(texto):
    n = numeros(texto)
    if not n:
        return None, None
    t = (texto or "").upper()
    return n[0], "CLF" if ("UF" in t or "CLF" in t) else "USD" if "USD" in t else "CLP"


MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]


def indicadores(cfg, previo):
    uf = previo.get("uf") or cfg["uf_respaldo"]
    dolar = previo.get("dolar") or cfg["dolar_respaldo"]
    try:
        uf = requests.get("https://mindicador.cl/api/uf", timeout=15).json()["serie"][0]["valor"]
        dolar = requests.get("https://mindicador.cl/api/dolar", timeout=15).json()["serie"][0]["valor"]
    except Exception:
        try:
            hoy = datetime.now()
            html = requests.get(f"https://www.sii.cl/valores_y_fechas/uf/uf{hoy.year}.htm", timeout=20).text
            mes = html.split(f"id='mes_{MESES[hoy.month - 1]}'", 1)[1].split("id='mes_", 1)[0]
            dias = dict(re.findall(r"<strong>(\d+)</strong></th>\s*<td[^>]*>([\d.,]+)</td>", mes))
            uf = numeros(dias[str(hoy.day)])[0]
        except Exception as e:
            log(f"No pude leer UF ({e.__class__.__name__}); uso {uf}")
    return uf, dolar


def a_uf(valor, moneda, uf, dolar):
    if valor is None:
        return None
    if moneda == "CLF":
        return float(valor)
    if moneda == "USD":
        return valor * dolar / uf
    return valor / uf


def escribir_seguro(destino, texto):
    """OneDrive bloquea a ratos el archivo mientras sincroniza: reintenta ~20 s."""
    tmp = destino.with_suffix(".tmp")
    tmp.write_text(texto, encoding="utf-8")
    for _ in range(20):
        try:
            tmp.replace(destino)
            return
        except PermissionError:
            time.sleep(1)
    destino.write_text(texto, encoding="utf-8")
    tmp.unlink(missing_ok=True)


CORREDOR_RE = re.compile(
    r"propiedad|corredor|inmobiliari|gesti[oó]n|asesor|broker|realty|real estate|propert|asociad|&| spa\b|ltda|"
    r"limitada|s\.a\.|group|grupo|arriendo|inversion|capital|homes?\b|house|remax|re/max|century|engel|sotheby|"
    r"keller|toctoc|negocios|consultor|administra|servicios|partners|holding", re.I)


def clasificar_vendedor(v):
    if not v or not (v.get("nombre") or v.get("tipo_ml")):
        return None, None
    if v.get("tipo_ml") == "real_estate_agency" or re.search(r"corredora|tienda", (v.get("encabezado") or "").lower()):
        return "Corredor", None
    if CORREDOR_RE.search(v.get("nombre") or ""):
        return "Corredor", "publica como particular, pero el nombre es de corredora"
    return "Dueño", None


def proceso_vivo(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    codigo = ctypes.c_ulong()
    k.GetExitCodeProcess(h, ctypes.byref(codigo))
    k.CloseHandle(h)
    return codigo.value == 259


def tomar_lock():
    DATA.mkdir(exist_ok=True)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 90 * 60:
        try:
            pid = int(LOCK.read_text().strip())
        except (ValueError, OSError):
            pid = None
        if pid and proceso_vivo(pid):
            return False
    LOCK.write_text(str(os.getpid()))
    return True


def esperar_red(max_seg=120):
    t0 = time.time()
    while time.time() - t0 < max_seg:
        try:
            requests.head("https://www.portalinmobiliario.com", headers=HEADERS, timeout=10)
            return True
        except requests.RequestException:
            time.sleep(10)
    log(f"Sin internet despues de {max_seg} s")
    return False


def toast(titulo, texto):
    if os.name != "nt":
        return

    def x(s):
        return escape(s, quote=True).replace("'", "&apos;")
    xml = (f"<toast activationType='protocol' launch='{x(DASHBOARD.as_uri())}'><visual><binding template='ToastGeneric'>"
           f"<text>{x(titulo)}</text><text>{x(texto)}</text></binding></visual></toast>")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$d = New-Object Windows.Data.Xml.Dom.XmlDocument; $d.LoadXml(@'\n" + xml + "\n'@);"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($d))"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                       timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log(f"No pude mostrar notificacion: {e}")


def publicar_web(cfg):
    pub = cfg.get("publicar_github") or {}
    if not pub.get("activo") or not DASHBOARD.exists():
        return
    tmp = Path(tempfile.mkdtemp(prefix="locales_web_"))
    try:
        shutil.copy(DASHBOARD, tmp / "index.html")
        (tmp / ".nojekyll").write_text("")
        git = ["git", "-C", str(tmp), "-c", "user.name=buscador", "-c", "user.email=buscador@users.noreply.github.com"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for args in (["init", "-q", "-b", "gh-pages"], ["add", "-A"], ["commit", "-qm", f"dashboard {ahora()}"],
                     ["push", "-qf", f"https://github.com/{pub['repo']}.git", "gh-pages"]):
            r = subprocess.run(git + args, capture_output=True, text=True, timeout=180, creationflags=flags)
            if r.returncode:
                log(f"No pude publicar en GitHub ({args[0]}): {r.stderr.strip()[:200]}")
                return
        log(f"Dashboard publicado en {pub.get('url', pub['repo'])}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- scraping

def parsear_tarjeta(pc):
    comp = {c.get("id"): c for c in pc.get("components", [])}
    precio = comp.get("price", {}).get("price", {}).get("current_price", {})
    fotos = [p["id"] for p in pc.get("pictures", {}).get("pictures", [])]
    md = pc["metadata"]
    return {
        "id": md["id"],
        "url": "https://" + md["url"].split("#")[0],
        "titulo": comp.get("title", {}).get("title", {}).get("text", ""),
        "direccion": comp.get("location", {}).get("location", {}).get("text", ""),
        "precio": precio.get("value"),
        "moneda": precio.get("currency", "CLP"),
        "atributos": comp.get("attributes_list", {}).get("attributes_list", {}).get("texts", []),
        "foto": URL_FOTO.format(id=fotos[0]) if fotos else None,
    }


def buscar(cli, cfg, b, op):
    """Une los resultados filtrando por superficie util y por superficie total (cada aviso trae una u otra)."""
    tol = cfg["tolerancia_busqueda"]
    a, z = round(b["m2_min"] * (1 - tol)), round(b["m2_max"] * (1 + tol))
    tarjetas, completa = {}, True
    for campo in ("COVERED", "TOTAL"):
        url = URL_BUSQUEDA.format(op=op, tipo=cfg["tipo_portal"], slug=b["slug"], campo=campo, a=a, b=z)
        paginas = 0
        while url and paginas < 10:
            paginas += 1
            html = cli.get(url)
            if html is None:
                break
            st = estado_nordic(html)
            for r in st.get("results", []):
                if "polycard" in r:
                    t = parsear_tarjeta(r["polycard"])
                    tarjetas[t["id"]] = t
            nxt = (st.get("pagination") or {}).get("next_page") or {}
            url = nxt.get("url") if nxt.get("show") else None
        if url:
            completa = False
    log(f"  {b['ciudad']} {op}: {len(tarjetas)} avisos entre {a} y {z} m²")
    return list(tarjetas.values()), completa


def parsear_detalle(html):
    st = estado_nordic(html)
    specs, lat, lon = {}, None, None
    for o in recorrer(st):
        if lat is None and "latitude" in o and "longitude" in o:
            try:
                lat, lon = float(o["latitude"]), float(o["longitude"])
            except (TypeError, ValueError):
                pass
        i, t = o.get("id"), o.get("text")
        if isinstance(i, str) and isinstance(t, str) and i[:1].isupper() and len(t) < 80:
            specs.setdefault(i, t)
    comp = st.get("components", {})
    sp_vend = comp.get("seller_profile") or {}
    tipo_ml = next((o["seller_type"] for o in recorrer(st) if isinstance(o.get("seller_type"), str)), None)
    fotos = list(dict.fromkeys(re.findall(r'"(\d{5,7}-MLC\d+_\d+)"', json.dumps(comp.get("gallery_mosaic", {})))))
    return {
        "lat": lat, "lon": lon, "specs": specs,
        "descripcion": ((comp.get("description") or {}).get("content", "") or "")[:2500],
        "fotos": [URL_FOTO.format(id=f) for f in fotos[:8]],
        "vendedor": {
            "nombre": ((sp_vend.get("seller_name") or {}).get("title") or {}).get("text", ""),
            "encabezado": (sp_vend.get("header") or {}).get("text", ""),
            "tipo_ml": tipo_ml,
        },
    }


# ---------------------------------------------------------------- evaluacion

TIPOS = [  # orden de prioridad: el primero que calce
    ("Strip center", r"strip|mini ?mall|power ?center|centro comercial vecinal|centro vecinal"),
    ("Mall / galería", r"\bmall\b|galer[ií]a|outlet|patio de comidas"),
    ("Placa comercial", r"placa|primer piso de(l)? edificio|z[oó]calo|nivel calle|local en edificio|base de edificio"),
    ("Stand alone", r"stand ?alone|edificio (comercial|completo|independiente)|propiedad comercial|local independiente|"
                    r"inmueble comercial|casa comercial|terreno con construcci"),
]
PUNTOS_TIPO = {"Strip center": 100, "Stand alone": 100, "Placa comercial": 80, "Local / otro": 50, "Mall / galería": 40,
               "Galpón / bodega": 30, "Derecho de llave": 0}
# Se miran solo en el titulo: en la descripcion "bodega" o "llave" aparecen en cualquier local
TIPOS_TITULO = [
    ("Derecho de llave", r"derecho (a|de) llave|traspaso de negocio|se vende negocio"),
    ("Galpón / bodega", r"galp[oó]n|bodega|nave industrial|centro de bodegaje"),
]
AVENIDA_RE = re.compile(r"\bav(\.|da\.?|enida)?\s|\bavda\b|\bavenida\b|\bruta\b|\bcamino\b|\balameda\b|\bcarretera\b", re.I)


def clasificar_tipo(a):
    for nombre, patron in TIPOS_TITULO:
        if re.search(patron, a["titulo"].lower()):
            return nombre
    texto = f"{a['titulo']} {a.get('descripcion', '')}".lower()
    for nombre, patron in TIPOS:
        if re.search(patron, texto):
            return nombre
    return "Local / otro"


def lineal(x, cero, cien):
    return max(0.0, min(100.0, (x - cero) / (cien - cero) * 100))


def mediana(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def evaluar(a, b, cfg, uf, dolar):
    sp = a.get("specs", {})
    util = (numeros(sp.get("Superficie útil")) or [None])[0]
    total = (numeros(sp.get("Superficie total")) or [None])[0]
    if not util and not total:  # sin ficha: lo que diga la tarjeta
        for t in a.get("atributos", []):
            if "m²" in t:
                util = numeros(t)[0]
    a["m2_util"], a["m2_total"] = util or None, total or None
    a["m2"] = util or total
    m2 = a["m2"]

    est = numeros(sp.get("Estacionamientos"))
    if not est:
        m = re.search(r"(\d{1,3})\s*(estacionamientos|estac\.|estacionamiento)", (a.get("descripcion") or "").lower())
        est = [float(m.group(1))] if m else []
    a["estacionamientos"] = int(est[0]) if est else None
    a["banos"] = int(numeros(sp.get("Baños"))[0]) if numeros(sp.get("Baños")) else None
    a["pisos"] = sp.get("Cantidad de pisos")
    a["antiguedad"] = sp.get("Antigüedad")
    a["tipo"] = clasificar_tipo(a)
    a["publica"], a["publica_nota"] = clasificar_vendedor(a.get("vendedor"))

    a["precio_uf"] = round(a_uf(a["precio"], a["moneda"], uf, dolar), 1) if a.get("precio") else None
    gv, gm = monto(sp.get("Gastos comunes"))
    a["ggcc_uf"] = round(a_uf(gv, gm, uf, dolar), 1) if gv else None
    a["uf_m2"] = round(a["precio_uf"] / m2, 3) if a["precio_uf"] and m2 else None
    lim = (0.05, 2.5) if a["operacion"] == "arriendo" else (3, 400)
    a["dudoso"] = a["uf_m2"] is not None and not (lim[0] <= a["uf_m2"] <= lim[1])

    tol = cfg["tolerancia_cercano"]
    if not m2:
        a["calce"] = "sin m²"
    elif b["m2_min"] <= m2 <= b["m2_max"]:
        a["calce"] = "calza"
    elif b["m2_min"] * (1 - tol) <= m2 <= b["m2_max"] * (1 + tol):
        a["calce"] = "cercano"
    else:
        a["calce"] = "fuera"
    a["frente_avenida"] = bool(AVENIDA_RE.search(f"{a['direccion']} {a['titulo']}"))
    a["esquina"] = "esquina" in f"{a['titulo']} {a.get('descripcion', '')}".lower()
    return a


def analizar(avisos, cfg):
    pesos = {k: v for k, v in cfg["pesos_puntaje"].items() if not k.startswith("_")}
    grupos = {}
    for a in avisos.values():
        if a.get("activo") and a.get("uf_m2") and not a["dudoso"]:
            grupos.setdefault((a["ciudad"], a["operacion"]), []).append(a["uf_m2"])
    por_op = {}
    for (c, op), xs in grupos.items():
        por_op.setdefault(op, []).extend(xs)
    b_por_ciudad = {b["ciudad"]: b for b in cfg["busquedas"]}
    for a in avisos.values():
        if not a.get("activo"):
            continue
        comps = grupos.get((a["ciudad"], a["operacion"]), [])
        ref = comps if len(comps) >= 5 else por_op.get(a["operacion"], [])
        med = mediana(ref)
        a["mercado_uf_m2"] = round(med, 3) if med else None
        a["comps"] = len(ref)
        a["vs_mercado"] = round(a["uf_m2"] / med - 1, 3) if a.get("uf_m2") and med and not a["dudoso"] else None

        b = b_por_ciudad[a["ciudad"]]
        m2 = a.get("m2")
        if not m2:
            calce = 0
        elif b["m2_min"] <= m2 <= b["m2_max"]:
            calce = 100
        else:
            lejos = (b["m2_min"] - m2) / b["m2_min"] if m2 < b["m2_min"] else (m2 - b["m2_max"]) / b["m2_max"]
            calce = lineal(lejos, 0.25, 0)
        det = {
            "precio_vs_mercado": lineal(a["vs_mercado"], 0.3, -0.3) if a["vs_mercado"] is not None else 50,
            "calce_m2": calce,
            "estacionamientos": lineal(a["estacionamientos"] or 0, 0, 15),
            "ubicacion": min(100, (70 if a["frente_avenida"] else 30) + (30 if a["esquina"] else 0)),
            "tipo": PUNTOS_TIPO.get(a["tipo"], 50),
        }
        a["puntaje_det"] = {k: round(v) for k, v in det.items()}
        a["puntaje"] = round(sum(det[k] * pesos[k] for k in det) / sum(pesos.values()))
        r = []
        if a["dudoso"]:
            r.append("⚠ revisar precio o m² (dato inusual)")
        elif a["vs_mercado"] is not None and abs(a["vs_mercado"]) >= 0.1:
            r.append(f"{abs(a['vs_mercado']):.0%} {'bajo' if a['vs_mercado'] < 0 else 'sobre'} la mediana de {a['ciudad']}")
        if a["frente_avenida"]:
            r.append("frente a avenida")
        if a["esquina"]:
            r.append("esquina")
        if a["estacionamientos"]:
            r.append(f"{a['estacionamientos']} estacionamientos")
        if a.get("m2_util") and a.get("m2_total") and a["m2_util"] > a["m2_total"] * 1.05:
            r.append("útil mayor que total: el total sería el terreno (varios pisos)")
        if a["tipo"] == "Derecho de llave":
            r.append("⚠ vende el negocio, no el inmueble")
        a["razones"] = r


# ---------------------------------------------------------------- salida

def escribir_dashboard(avisos, meta):
    payload = {"meta": meta, "avisos": sorted(avisos.values(), key=lambda a: -(a.get("puntaje") or 0))}
    datos = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    escribir_seguro(DASHBOARD, PLANTILLA.read_text(encoding="utf-8").replace("/*__DATOS__*/null", datos, 1))


def construir_meta(cfg, avisos, uf, previo, corrida):
    activos = [a for a in avisos.values() if a.get("activo")]
    return {
        "generado": ahora(), "uf": uf,
        "busquedas": cfg["busquedas"], "operaciones": cfg["operaciones"],
        "tolerancia_cercano": cfg["tolerancia_cercano"],
        "pesos": {k: v for k, v in cfg["pesos_puntaje"].items() if not k.startswith("_")},
        "activos": len(activos),
        "calzan": sum(1 for a in activos if a.get("calce") == "calza"),
        "carga_inicial_completa": previo.get("carga_inicial_completa", False),
        "corridas": (previo.get("corridas", []) + ([corrida] if corrida else []))[-50:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo-dashboard", action="store_true")
    args = ap.parse_args()

    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    DATA.mkdir(exist_ok=True)
    avisos = json.loads(AVISOS_JSON.read_text(encoding="utf-8")) if AVISOS_JSON.exists() else {}
    meta_previa = avisos.pop("_meta", {})
    b_por_ciudad = {b["ciudad"]: b for b in cfg["busquedas"]}

    if args.solo_dashboard:
        uf = meta_previa.get("uf", cfg["uf_respaldo"])
        for a in avisos.values():
            if a["ciudad"] in b_por_ciudad:
                evaluar(a, b_por_ciudad[a["ciudad"]], cfg, uf, cfg["dolar_respaldo"])
        analizar(avisos, cfg)
        meta = construir_meta(cfg, avisos, uf, meta_previa, None)
        escribir_seguro(AVISOS_JSON, json.dumps({**avisos, "_meta": meta}, ensure_ascii=False, indent=1))
        escribir_dashboard(avisos, meta)
        log("dashboard.html regenerado")
        publicar_web(cfg)
        return

    log(f"Arranque (pid {os.getpid()})")
    if not tomar_lock():
        log("Otra ejecucion sigue corriendo; salgo")
        return
    vigilante = threading.Timer(50 * 60, lambda: (log("Pasada colgada; la cierro"), LOCK.unlink(missing_ok=True), os._exit(1)))
    vigilante.daemon = True
    vigilante.start()
    t0 = time.time()
    try:
        if not esperar_red():
            return
        carga_inicial = not meta_previa.get("carga_inicial_completa")
        uf, dolar = indicadores(cfg, meta_previa)
        cli = Cliente(cfg["pausa_entre_requests_seg"])
        log(f"Inicio busqueda locales (UF {uf:,.0f})")
        vistos, completas, bloqueado, nuevos, fichas = set(), True, None, [], 0
        limite = datetime.now() - timedelta(days=cfg["refrescar_detalle_cada_dias"])
        try:
            for b in cfg["busquedas"]:
                for op in cfg["operaciones"]:
                    tarjetas, ok = buscar(cli, cfg, b, op)
                    completas &= ok
                    for t in tarjetas:
                        k = t["id"]
                        vistos.add(k)
                        prev = avisos.get(k)
                        a = {**(prev or {}), **t, "ciudad": b["ciudad"], "operacion": op,
                             "activo": True, "ultima_vez": ahora()}
                        a.setdefault("primera_vez", ahora())
                        if prev is None:
                            a["carga_inicial"] = carga_inicial
                            a["historial_precio"] = [[ahora()[:10], t["precio"], t["moneda"]]]
                        elif (prev.get("precio"), prev.get("moneda")) != (t["precio"], t["moneda"]):
                            a["historial_precio"] = prev.get("historial_precio", []) + [[ahora()[:10], t["precio"], t["moneda"]]]
                        if not a.get("detalle_en") or datetime.fromisoformat(a["detalle_en"]) < limite:
                            html = cli.get(t["url"])
                            if html:
                                try:
                                    a.update(parsear_detalle(html))
                                    a["detalle_en"] = ahora()
                                    fichas += 1
                                except Bloqueado:
                                    raise
                                except Exception as e:
                                    log(f"  no pude leer ficha {k}: {e.__class__.__name__}")
                        calzaba = bool(prev and prev.get("calce") == "calza")
                        evaluar(a, b, cfg, uf, dolar)
                        if a["calce"] == "calza" and not calzaba:
                            nuevos.append(a)
                        avisos[k] = a
        except Bloqueado as e:
            bloqueado, completas = str(e), False
            log(f"BLOQUEO: {e}")

        bajas = 0
        if completas:
            for k, a in avisos.items():
                if k not in vistos and a.get("activo"):
                    a["activo"], a["baja_en"] = False, ahora()
                    bajas += 1
        analizar(avisos, cfg)
        corrida = {"fecha": ahora(), "segundos": round(time.time() - t0), "avisos": len(vistos), "fichas": fichas,
                   "nuevos": len(nuevos), "dados_de_baja": bajas, "bloqueo": bloqueado}
        meta = construir_meta(cfg, avisos, uf, meta_previa, corrida)
        meta["carga_inicial_completa"] = meta_previa.get("carga_inicial_completa") or (completas and not bloqueado)
        escribir_seguro(AVISOS_JSON, json.dumps({**avisos, "_meta": meta}, ensure_ascii=False, indent=1))
        escribir_dashboard(avisos, meta)
        log(f"Fin: {len(vistos)} avisos, {fichas} fichas, {meta['calzan']} calzan, {len(nuevos)} nuevos que calzan, "
            f"{bajas} dados de baja ({corrida['segundos']} s)")
        if nuevos and cfg["notificaciones"].get("windows", True):
            por_ciudad = {}
            for a in nuevos:
                por_ciudad[a["ciudad"]] = por_ciudad.get(a["ciudad"], 0) + 1
            titulo = ("Carga inicial: " if carga_inicial else "") + f"{len(nuevos)} locales que calzan"
            toast(titulo, ", ".join(f"{c} {n}" for c, n in por_ciudad.items()))
        publicar_web(cfg)
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR no controlado: {e.__class__.__name__}: {e}")
        raise
