/* Los logos, en UN solo lugar. Los usan dos paginas -- /quien-cargo (template Jinja) y
   map.html (archivo suelto, servido con send_file) -- y no comparten forma de incluir
   nada, asi que la unica manera de no tener dos copias es un .js en static/ que las dos
   carguen. Este repo ya sabe como termina la duplicacion: dos acumuladores del mismo
   numero dando resultados distintos y nadie sabiendo cual creer.

   NO SALE DE INTERNET: los archivos viven en static/logos/, igual que Plotly y el mapa
   base (regla 2). Un logo traido de un CDN desaparece justo en la PC sin red. */

/* Nombre COMPLETO con extension: conviven .png (los oficiales que aporto YPF) y .svg
   (los que faltaban, dibujados a mano). Pegarle una extension fija al nombre obligaria a
   convertir archivos para que entren, que es trabajo por una decision del codigo. */
const LOGO_PROVEEDOR = { YPF: 'ypf.png', RAIZEN: 'shell.png', AXION: 'axion.png' };

/* Solo las que mas vuelan. Dibujar las 37 que aparecen en los datos seria trabajo sin
   retorno -- la cola son una o dos partidas cada una -- y las que faltan simplemente no
   muestran logo. Si el codigo no esta aca NO se pide el archivo, para no generar un 404
   por cada fila de una aerolinea sin logo. */
const LOGO_AEROLINEA = { AR: 'ar.png', WJ: 'wj.png',
                         H2: 'h2.svg', G3: 'g3.svg', CM: 'cm.svg', O4: 'o4.svg',
                         '5U': '5u.svg',
                         // Las tres LATAM comparten marca: son la misma empresa con
                         // filial distinta, y el logo que se ve en la pista es el mismo.
                         LA: 'la.svg', JJ: 'jj.svg', LP: 'lp.svg' };

// Escape propio: este archivo lo cargan dos paginas y no puede depender del `esc()` de
// ninguna. Los codigos IATA vienen de una fuente ajena.
function _logoEsc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* onerror se saca a si mismo: mientras un archivo no este, no queda el icono roto del
   navegador sino nada, y el texto que acompania al logo sigue diciendo el dato. */
function logoProveedor(prov) {
  const f = LOGO_PROVEEDOR[String(prov || '').toUpperCase()];
  if (!f) return '';
  return `<img class="logo-prov" src="/static/logos/${f}" alt="${_logoEsc(prov)}"`
       + ` onerror="this.remove()">`;
}

function logoAerolinea(id) {
  const f = LOGO_AEROLINEA[String(id || '').trim().toUpperCase()];
  if (!f) return '';
  return `<img class="logo-aero" src="/static/logos/${f}" alt="${_logoEsc(id)}"`
       + ` onerror="this.remove()">`;
}
