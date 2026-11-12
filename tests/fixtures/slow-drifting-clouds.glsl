// Soft clouds that drift at 0.001 units/s: too slow to show between t = 0 and 2.5 s.
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }

float noise(vec2 p){
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1,0)), u.x),
               mix(hash(i + vec2(0,1)), hash(i + vec2(1,1)), u.x), u.y);
}

float fbm(vec2 p){
    float v = 0.0, a = 0.5;
    for (int k = 0; k < 4; k++){ v += a * noise(p); p = p * 2.0 + vec2(1.7, 9.2); a *= 0.5; }
    return v;
}

void mainImage(out vec4 fragColor, in vec2 fragCoord){
    vec2 uv = fragCoord / iResolution.xy;
    vec2 p = vec2(uv.x * iResolution.x / iResolution.y + iTime * 0.001, uv.y) * 2.0;
    float c = smoothstep(0.35, 0.75, fbm(p));
    vec3 sky = mix(vec3(0.42, 0.62, 0.86), vec3(0.20, 0.36, 0.66), uv.y);
    fragColor = vec4(mix(sky, vec3(0.95), c * 0.8), 1.0);
}
