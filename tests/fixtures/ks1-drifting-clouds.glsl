// @slider sunHeight 0.0..1.0 = 0.22 step 0.01 "Sun height"
// @slider cloudCover 0.0..1.0 = 0.45 step 0.01 "Cloud cover"
// @slider warmth 0.0..1.0 = 0.7 step 0.01 "Warmth"

float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }

float noise(vec2 p){
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1,0)), u.x),
               mix(hash(i + vec2(0,1)), hash(i + vec2(1,1)), u.x), u.y);
}

float fbm(vec2 p){
    float v = 0.0, a = 0.5;
    for (int k = 0; k < 5; k++){ v += a * noise(p); p = p * 2.03 + vec2(1.7, 9.2); a *= 0.5; }
    return v;
}

void mainImage(out vec4 fragColor, in vec2 fragCoord){
    vec2 uv = fragCoord / iResolution.xy;
    float aspect = iResolution.x / iResolution.y;
    vec2 p = vec2((uv.x - 0.5) * aspect, uv.y);

    // Sun position: low sun sits near the horizon line at y = 0.18
    float horizon = 0.18;
    vec2 sunPos = vec2(0.125 * aspect, mix(horizon - 0.04, 0.85, sunHeight));
    float d = length(p - sunPos);

    // Palette: warmth shifts from pale cool dusk to deep amber
    float low = 1.0 - sunHeight;
    vec3 zenithCool = vec3(0.18, 0.32, 0.62);
    vec3 zenithWarm = vec3(0.30, 0.22, 0.46);
    vec3 horizCool  = vec3(0.95, 0.82, 0.66);
    vec3 horizWarm  = vec3(1.25, 0.50, 0.12);
    vec3 zenith = mix(zenithCool, zenithWarm, warmth * low);
    vec3 horiz  = mix(horizCool, horizWarm, warmth);
    vec3 sunCol = mix(vec3(1.0, 0.92, 0.75), vec3(1.0, 0.58, 0.20), warmth);

    // Sky gradient
    float h = clamp((uv.y - horizon) / (1.0 - horizon), 0.0, 1.0);
    vec3 sky = mix(horiz, zenith, pow(h, 0.5));

    // Sun glow and a hot, saturated disc
    float glow = exp(-d * 4.0) * 0.7 + exp(-d * 14.0) * 0.9;
    sky += sunCol * glow;
    float disc = 1.0 - smoothstep(0.042, 0.05, d);
    sky = mix(sky, sunCol * 4.0 + vec3(0.6, 0.5, 0.3), disc);

    // Clouds: drifting fbm, denser with cover, lit from the sun side
    vec2 cp = vec2(p.x * 1.6 + iTime * 0.08, (uv.y - horizon) * 4.0);
    cp.x /= max(0.25, h + 0.25);
    float n = fbm(cp * 2.2);
    float thr = mix(0.72, 0.30, cloudCover);
    float c = smoothstep(thr, thr + 0.2, n) * smoothstep(0.0, 0.08, uv.y - horizon);
    float shade = fbm(cp * 2.2 + vec2(0.05, -0.05));
    float lit = clamp(0.6 + (n - shade) * 6.0, 0.0, 1.0);
    vec3 cloudDark = mix(vec3(0.38, 0.33, 0.45), vec3(0.45, 0.24, 0.30), warmth);
    vec3 cloudLit  = mix(horiz * 1.05, sunCol, 0.5) + sunCol * exp(-d * 3.0) * 0.8;
    vec3 cloud = mix(cloudDark, cloudLit, lit);
    sky = mix(sky, cloud, c * 0.9);

    // Ground silhouette below the horizon with a soft haze line
    float hill = horizon + 0.03 * fbm(vec2(p.x * 3.0, 1.0)) - 0.015;
    vec3 ground = mix(vec3(0.06, 0.04, 0.06), horiz * 0.25, warmth * 0.3);
    float g = 1.0 - smoothstep(hill - 0.003, hill + 0.003, uv.y);
    sky = mix(sky, ground, g);
    sky += horiz * 0.3 * exp(-abs(uv.y - hill) * 40.0) * (1.0 - g);

    // Tone map and dither against banding
    vec3 col = 1.0 - exp(-sky * 1.5);
    col += (hash(fragCoord + fract(iTime)) - 0.5) / 255.0;
    fragColor = vec4(col, 1.0);
}
