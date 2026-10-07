"use strict";

function placeCoordinates(place) {
  const {latitude: lat, longitude: lon} = place;
  return typeof lat === "number" && typeof lon === "number" && Number.isFinite(lat) && Number.isFinite(lon)
    && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? [lat, lon] : null;
}

const placeMap = {
  map: null,
  layers: null,
  places: null,
  markers: new Map(),
  buttons: new Map(),
  selected: null,

  init(config = {}) {
    this.center = config.center || [16, 106];
    this.zoom = config.zoom ?? 5;
    if (!window.L) {
      $("map-network-status").hidden = false;
      return;
    }
    this.map = L.map("places-map", {zoomControl: false}).setView(this.center, this.zoom);
    L.control.zoom({position: "topright"}).addTo(this.map);
    const attribution = DOMPurify.sanitize(config.attribution || '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', {
      ALLOWED_TAGS: ["a"], ALLOWED_ATTR: ["href"],
    });
    L.tileLayer(config.tile_url || "https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19, attribution})
      .on("tileerror", () => { $("map-network-status").hidden = false; })
      .on("tileload", () => { $("map-network-status").hidden = true; })
      .addTo(this.map);
    this.layers = L.layerGroup().addTo(this.map);
    new ResizeObserver(() => {
      this.map.invalidateSize({pan: false});
      if (this.needsFit && $("places-map").clientWidth) this.fit();
    }).observe($("places-map"));
  },

  show(places = []) {
    if (this.places === places) return;
    this.places = places;
    this.layers?.clearLayers();
    this.markers.clear();
    this.buttons.clear();
    this.selected = null;
    $("map-results").replaceChildren();
    $("place-detail").hidden = true;
    places.forEach((place, index) => {
      const number = place.result_number || index + 1;
      const coordinates = placeCoordinates(place);
      const button = element("button", "map-place", `${number}. ${place.name || "Unnamed place"}`);
      button.type = "button";
      button.title = coordinates ? place.name : `${place.name} · Coordinates unavailable`;
      button.onclick = () => this.select(place);
      this.buttons.set(place, button);
      $("map-results").append(button);
      if (coordinates && this.map) {
        const marker = L.marker(coordinates, {
          icon: L.divIcon({className: "place-marker", html: element("span", "map-pin", String(number)), iconSize: [30, 30], iconAnchor: [15, 15]}),
          title: place.name || "Retrieved place", keyboard: true,
        }).addTo(this.layers);
        marker.bindTooltip(element("span", "", place.name || "Retrieved place"), {direction: "top", offset: [0, -16]});
        marker.on("click", () => this.select(place, false));
        this.markers.set(place, marker);
      }
    });
    const pins = places.filter(placeCoordinates).length;
    $("place-count").textContent = places.length ? `${places.length} place${places.length === 1 ? "" : "s"} · ${pins} with coordinates` : "No places in this response";
    $("fit-places").disabled = !this.markers.size;
    $("map-empty").hidden = pins > 0;
    $("map-empty").textContent = places.length ? "Coordinates are unavailable. Select a place to inspect its details." : "No places to show. Search for a destination or describe a place you'd like to visit.";
    this.fit();
    if (places.length) this.select(places[0], false);
  },

  fit() {
    if (!this.map) return;
    if (!$("places-map").clientWidth) { this.needsFit = true; return; }
    this.needsFit = false;
    const coordinates = (this.places || []).map(placeCoordinates).filter(Boolean);
    if (coordinates.length) this.map.fitBounds(L.latLngBounds(coordinates), {paddingTopLeft: [35, 90], paddingBottomRight: [35, 210], maxZoom: 13, animate: false});
    else this.map.setView(this.center, this.zoom, {animate: false});
  },

  select(place, pan = true) {
    this.selected = place;
    for (const [item, button] of this.buttons) {
      button.classList.toggle("selected", item === place);
      button.setAttribute("aria-pressed", String(item === place));
    }
    for (const [item, marker] of this.markers) {
      marker.getElement()?.classList.toggle("selected", item === place);
      marker.setZIndexOffset(item === place ? 1000 : 0);
    }
    const coordinates = placeCoordinates(place);
    if (pan && coordinates && this.map) this.map.setView(coordinates, Math.max(this.map.getZoom(), 12), {animate: !matchMedia("(prefers-reduced-motion: reduce)").matches});
    const detail = $("place-detail");
    detail.replaceChildren();
    detail.hidden = false;
    const overview = element("div", "place-overview");
    const photo = place.image;
    if (photo?.image_url) {
      const image = element("img", "place-photo");
      image.src = photo.image_url;
      image.alt = place.name || "Retrieved place photo";
      image.onerror = () => image.replaceWith(element("div", "place-photo photo-unavailable", "Photo unavailable"));
      overview.append(image);
    }
    const info = element("div", "place-info");
    info.append(element("h2", "", `${place.result_number || this.places.indexOf(place) + 1}. ${place.name || "Unnamed place"}`));
    info.append(element("p", "place-source", place.source === "text" ? "Wikivoyage destination" : "Photo match · scraped place metadata"));
    const labels = [place.category, place.province].filter(Boolean);
    if (labels.length) info.append(element("p", "", labels.join(" · ")));
    const address = typeof place.address === "object" && place.address ? Object.values(place.address).filter(Boolean).join(", ") : place.address;
    if (address) info.append(element("p", "", address));
    info.append(element("p", "place-coordinates", coordinates ? `${coordinates[0].toFixed(6)}, ${coordinates[1].toFixed(6)}` : "Coordinates unavailable"));
    overview.append(info);
    detail.append(overview);
    if (place.excerpt) detail.append(element("p", "place-excerpt", place.excerpt));
    const links = element("div", "place-links");
    for (const [label, value] of [["Place source", place.url], ["Photo source", photo?.source_url]]) {
      const url = safeUrl(value);
      if (url) {
        const link = element("a", "", label);
        link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
        links.append(link);
      }
    }
    if (links.childElementCount) detail.append(links);
    const attribution = photo?.attribution || photo?.author || "";
    const credit = photo?.license && !attribution.includes(photo.license) ? [attribution, photo.license].filter(Boolean).join(" · ") : attribution;
    if (credit) detail.append(element("p", "place-credit", credit));
  },
};
