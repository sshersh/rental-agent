window.dashExtensions = Object.assign({}, window.dashExtensions, {
    default: {
        function0: function(feature, latlng, context) {
                if (!window._mainLeafletMap) window._mainLeafletMap = context.map;
                return L.circleMarker(latlng, {
                    radius: 5,
                    fillColor: '#e53935',
                    color: '#ff8a80',
                    weight: 0.5,
                    fillOpacity: 0.85,
                });
            }

            ,
        function1: function(feature, latlng, context) {
                const m = L.circleMarker(latlng, {
                    radius: 11,
                    fillColor: '#FFD700',
                    color: '#fff',
                    weight: 2.5,
                    fillOpacity: 1,
                });
                const addr = feature.properties && feature.properties.address;
                if (addr) {
                    m.bindTooltip(addr, {
                        direction: 'top',
                        offset: [0, -8],
                        className: 'selected-bldg-tooltip',
                        sticky: false,
                    });
                }
                return m;
            }

            ,
        function2: function(feature, latlng, context) {
                const n = feature.properties.point_count;
                const label = feature.properties.point_count_abbreviated;
                const size = Math.min(72, Math.max(26, 18 + Math.sqrt(n) * 4.5));
                const inner = size - 10;
                const icon = L.divIcon({
                    html: '<div style="width:' + inner + 'px;height:' + inner + 'px;' +
                        'line-height:' + inner + 'px;margin:5px;border-radius:50%;' +
                        'text-align:center;"><span>' + label + '</span></div>',
                    className: 'marker-cluster marker-cluster-proportional',
                    iconSize: L.point(size, size),
                });
                return L.marker(latlng, {
                    icon: icon
                });
            }

            ,
        function3: function(feature, latlng, context) {
                const colors = {
                    '1': '#EE352E',
                    '2': '#EE352E',
                    '3': '#EE352E',
                    '4': '#00933C',
                    '5': '#00933C',
                    '6': '#00933C',
                    '7': '#B933AD',
                    'A': '#0039A6',
                    'C': '#0039A6',
                    'E': '#0039A6',
                    'B': '#FF6319',
                    'D': '#FF6319',
                    'F': '#FF6319',
                    'M': '#FF6319',
                    'G': '#6CBE45',
                    'J': '#996633',
                    'Z': '#996633',
                    'L': '#A7A9AC',
                    'N': '#FCCC0A',
                    'Q': '#FCCC0A',
                    'R': '#FCCC0A',
                    'W': '#FCCC0A',
                    'S': '#808183',
                    'GS': '#808183',
                    'FS': '#808183',
                };
                const darkText = new Set(['N', 'Q', 'R', 'W']);
                const lines = feature.properties.lines || [];
                if (!lines.length) {
                    return L.circleMarker(latlng, {
                        radius: 10,
                        fillColor: '#808183',
                        color: '#fff',
                        weight: 1.5,
                        fillOpacity: 1
                    });
                }
                const bubbles = lines.map(line => {
                    const bg = colors[line] || '#808183';
                    const fg = darkText.has(line) ? '#000' : '#fff';
                    return '<span style="display:inline-flex;align-items:center;justify-content:center;' +
                        'width:20px;height:20px;border-radius:50%;background:' + bg + ';color:' + fg + ';' +
                        'font-size:11px;font-weight:bold;font-family:Arial,sans-serif;' +
                        'border:1.5px solid rgba(255,255,255,0.85);' +
                        'box-shadow:0 1px 4px rgba(0,0,0,0.55);flex-shrink:0;">' + line + '</span>';
                });
                const perRow = Math.min(lines.length, 4);
                const rows = Math.ceil(lines.length / perRow);
                const w = perRow * 22;
                const h = rows * 22;
                const html = '<div style="display:flex;flex-wrap:wrap;gap:2px;width:' + w + 'px;">' +
                    bubbles.join('') + '</div>';
                return L.marker(latlng, {
                    icon: L.divIcon({
                        html: html,
                        className: '',
                        iconSize: [w, h],
                        iconAnchor: [w / 2, h / 2],
                        popupAnchor: [0, -h / 2 - 4],
                    })
                });
            }

            ,
        function4: function(feature, layer, context) {
                const name = feature.properties.name || '';
                const lines = (feature.properties.lines || []).join(' ');
                if (name) layer.bindTooltip(name + (lines ? ' (' + lines + ')' : ''), {
                    sticky: true,
                    className: 'subway-tooltip'
                });
            }

            ,
        function5: function(feature, context) {
                const colors = {
                    '1': '#EE352E',
                    '2': '#EE352E',
                    '3': '#EE352E',
                    '4': '#00933C',
                    '5': '#00933C',
                    '6': '#00933C',
                    '7': '#B933AD',
                    'A': '#0039A6',
                    'C': '#0039A6',
                    'E': '#0039A6',
                    'B': '#FF6319',
                    'D': '#FF6319',
                    'F': '#FF6319',
                    'M': '#FF6319',
                    'G': '#6CBE45',
                    'J': '#996633',
                    'Z': '#996633',
                    'L': '#A7A9AC',
                    'N': '#FCCC0A',
                    'Q': '#FCCC0A',
                    'R': '#FCCC0A',
                    'W': '#FCCC0A',
                    'S': '#808183',
                };
                const line = feature.properties.primary_line || 'S';
                return {
                    color: colors[line] || '#808183',
                    weight: 3,
                    opacity: 0.75,
                    fillOpacity: 0
                };
            }

            ,
        function6: function(feature, context) {
                if (!window._mainLeafletMap) window._mainLeafletMap = context.map;
                return {
                    color: '#4a6fa5',
                    weight: 1.5,
                    fill: false,
                    dashArray: '4'
                };
            }

            ,
        function7: function(feature, layer, context) {
            const code = feature.properties.modzcta ||
                feature.properties.ZIPCODE ||
                feature.properties.zipcode ||
                feature.properties.ZIP || '';
            layer.bindTooltip(String(code), {
                sticky: true,
                className: 'zip-tooltip'
            });
        }

    }
});