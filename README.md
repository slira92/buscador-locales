# Buscador Locales Comerciales

Busca en Portal Inmobiliario ubicaciones comerciales (strip centers, stand alone y placas comerciales)
para un requerimiento multi-ciudad, en **arriendo y venta**, y las rankea en un dashboard.

## Requerimiento actual (`config.json` → `busquedas`)

| Ciudad | Pedido | "Calza" | "Cercano" (±15%) |
|---|---|---|---|
| Concepción | 900–1.000 m² | 900–1.000 | 765–1.150 |
| Recoleta | 1.100–1.200 m² | 1.100–1.200 | 935–1.380 |
| Temuco | 1.000 m² | 900–1.100 | 765–1.265 |
| Viña del Mar | 1.000 m² | 900–1.100 | 765–1.265 |
| Huechuraba | 900 m² | 810–990 | 690–1.140 |

## Cómo funciona

1. Para cada ciudad y operación busca en la categoría "comercial" con un rango de m² ampliado
   (±35%), filtrando por superficie útil y por superficie total, porque cada aviso informa una u otra.
2. Entra a cada ficha: superficie útil y total, estacionamientos, pisos, gastos comunes, coordenadas,
   fotos, descripción y quién publica.
3. Clasifica:
   - **Tipo**, por título y descripción: Strip center, Stand alone, Placa comercial, Mall/galería o Local/otro.
   - **Calce de m²**: calza, cercano o fuera.
   - **Quién publica**: dueño o corredor (tipo de cuenta + nombre).
4. Calcula **UF/m²** (los avisos en pesos se pasan a UF con la UF del día) y lo compara con la mediana
   de la misma ciudad y operación.
5. **Puntaje 0–100** (pesos en `config.json` → `pesos_puntaje`):
   - precio vs mercado (35%)
   - calce de m² (25%)
   - estacionamientos (15%; 15 o más = 100)
   - ubicación (15%; frente a avenida/ruta, esquina)
   - tipo (10%; strip/stand alone 100, placa 80, local 50, mall 40)

Ojo: la superficie de un stand alone a veces corresponde al terreno. Revisa siempre útil vs total en la ficha.

## Dashboard

`dashboard.html`. Vista **Resumen** por ciudad (calzan, cercanos, mediana UF/m², top 4 por operación),
**Tarjetas**, **Tabla** y mapa. Filtros por ciudad, operación, calce, tipo, estacionamientos y "solo dueños".
Favoritos, descartados y notas se guardan en el navegador.

## Comandos

```bash
py buscador_locales.py                    # búsqueda completa
py buscador_locales.py --solo-dashboard   # recalcula con config.json sin scrapear
powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1               # automático 09:15, 13:15, 17:15
powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1 -Desinstalar
```

## Publicar en un link

`config.json` → `publicar_github`: `activo: true`, `repo: "usuario/repo"` y `url`. Sube `dashboard.html`
a la rama `gh-pages` del repo después de cada pasada, igual que el buscador de deptos.
