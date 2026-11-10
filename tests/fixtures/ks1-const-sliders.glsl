// Golden-hour sky
// @slider sunHeight 0.0..1.0 = 0.22 step 0.01
// @slider cloudCover 0.0..1.0 = 0.45 step 0.01
// @slider warmth 0.0..1.0 = 0.65 step 0.01
// The live shader_render does not inject @slider uniforms, so the defaults are
// pinned here. Delete these three lines once a slider-aware renderer is loaded.
const float sunHeight = 0.22;
const float cloudCover = 0.45;
const float warmth = 0.65;

float hash(vec2 p) {
    p = fract(p * vec2(127.1, 311.7));
    p += dot(p, p + 34.23);
    return fract(p.x * p.y);
}

float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), u.x),
               mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), u.x), u.y);
}

float fbm(vec2 p) {
    float v = 0.0, a = 0.5;
    mat2 r = mat2(0.8, 0.6, -0.6, 0.8);
    for (int i = 0; i < 5; i++) {
        v += a * noise(p);
        p = r * p * 2.03 + 11.3;
        a *= 0.5;
    }
    return v;
}

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec2 p = (uv - 0.5) * vec2(iResolution.x / iResolution.y, 1.0);

    float sh = clamp(sunHeight, 0.0, 1.0);
    float cc = clamp(cloudCover, 0.0, 1.0);
    float wm = clamp(warmth, 0.0, 1.0);
    float low = 1.0 - smoothstep(0.0, 0.8, sh);

    float horizon = -0.28;
    vec2 sun = vec2(-0.12, horizon + mix(0.01, 0.65, sh));
    float d = length((p - sun) * vec2(0.8, 1.0));

    float y = clamp((p.y - horizon) / 0.8, 0.0, 1.0);
    vec3 zen = mix(vec3(0.10, 0.22, 0.48), vec3(0.22, 0.12, 0.34), wm) * mix(1.0, 0.8, low);
    vec3 mid = mix(vec3(0.55, 0.62, 0.80), vec3(0.96, 0.55, 0.40), wm);
    vec3 hor = mix(vec3(1.0, 0.84, 0.62), vec3(1.0, 0.38, 0.12), wm);
    mid = mix(mid, hor, low * 0.3);
    vec3 col = mix(hor, mid, smoothstep(0.0, 0.4, y));
    col = mix(col, zen, smoothstep(0.35, 1.0, y));

    vec3 tint = mix(vec3(1.0, 0.95, 0.80), vec3(1.0, 0.60, 0.25), wm);
    col += tint * (exp(-d * mix(5.0, 2.6, low)) * 0.7 + exp(-d * 1.2) * 0.2);
    float r = mix(0.05, 0.03, sh);
    col = mix(col, tint * 1.7 + 0.3, 1.0 - smoothstep(r * 0.85, r, d));

    float h = max(p.y - horizon, 0.02) + 0.15;
    vec2 q = vec2(p.x * 2.0 / h + iTime * 0.02, 0.6 / h);
    vec2 w = vec2(fbm(q * 0.5 + 3.1), fbm(q * 0.5 + 7.7));
    float n = fbm(q * 1.2 + w * 1.3);
    float thr = mix(0.82, 0.25, cc);
    float cloud = smoothstep(thr, thr + 0.18, n);
    cloud *= smoothstep(0.01, 0.1, p.y - horizon) * (1.0 - smoothstep(0.85, 1.05, y));
    vec2 sd = normalize(sun - p + 1e-4);
    float n2 = fbm((q + sd * 0.15) * 1.2 + w * 1.3);
    float lit = clamp(0.45 + (n - n2) * 7.0, 0.0, 1.0);
    float gl = exp(-d * 1.5);
    vec3 shade = mix(vec3(0.32, 0.25, 0.35), vec3(0.40, 0.17, 0.19), wm);
    vec3 bright = mix(vec3(1.0, 0.88, 0.70), vec3(1.0, 0.56, 0.30), wm);
    vec3 cCol = mix(shade, bright, lit * (0.35 + 0.65 * gl)) * (0.85 + 0.3 * y);
    col = mix(col, cCol, cloud * 0.9);

    float ridge = horizon + 0.05 * (fbm(vec2(p.x * 1.4 + 4.0, 1.0)) - 0.3);
    vec3 gc = mix(vec3(0.02, 0.02, 0.025), vec3(0.07, 0.035, 0.03), wm);
    gc += hor * 0.15 * exp(-max(p.y - ridge, 0.0) * 20.0);
    col = mix(col, gc, 1.0 - smoothstep(ridge - 0.002, ridge + 0.002, p.y));

    col = col / (1.0 + max(col - 0.75, 0.0) * 0.5);
    col *= 1.0 - 0.18 * dot(uv - 0.5, uv - 0.5);
    col += (hash(fragCoord + fract(iTime)) - 0.5) * 0.01;
    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
