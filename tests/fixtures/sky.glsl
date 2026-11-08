// Golden-hour sky, Shadertoy conventions (single Image pass, no channels).
// A warm vertical gradient, a low sun with a wide glow, and slow cloud bands.
// Everything is a pure function of fragCoord and iTime, so a frame at t = 1 s is reproducible.
// @slider sunHeight 0.05..0.95 = 0.32 step 0.01
// @slider cloudCover 0.0..1.0 = 0.55 step 0.01
// @slider warmth 0.0..1.0 = 0.75 step 0.01

float hash21(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float valueNoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float a = hash21(i);
  float b = hash21(i + vec2(1.0, 0.0));
  float c = hash21(i + vec2(0.0, 1.0));
  float d = hash21(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p) {
  float sum = 0.0;
  float amp = 0.5;
  for (int i = 0; i < 5; i++) {
    sum += amp * valueNoise(p);
    p *= 2.03;
    amp *= 0.5;
  }
  return sum;
}

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
  vec2 uv = fragCoord / iResolution.xy;
  float horizon = 0.46;

  // Sky gradient: deep blue overhead, warm haze at the horizon.
  vec3 zenith = vec3(0.09, 0.16, 0.36);
  vec3 haze = mix(vec3(0.95, 0.62, 0.35), vec3(1.0, 0.82, 0.52), warmth);
  float h = clamp((uv.y - horizon) / (1.0 - horizon), 0.0, 1.0);
  vec3 col = mix(haze, zenith, pow(h, 0.75));

  // The sun: a disc plus a two-scale glow.
  vec2 sunPos = vec2(0.5 + 0.18 * sin(iTime * 0.05), horizon + sunHeight * 0.42);
  float d = length((uv - sunPos) * vec2(iResolution.x / iResolution.y, 1.0));
  // Edges always ascending: GLSL leaves smoothstep undefined when edge0 > edge1, and the TSL
  // renderer draws such a frame black.
  float disc = 1.0 - smoothstep(0.046, 0.052, d);
  float glow = 0.55 * exp(-26.0 * d) + 0.22 * exp(-6.0 * d);
  vec3 sunColour = mix(vec3(1.0, 0.93, 0.72), vec3(1.0, 0.72, 0.36), warmth);
  col += sunColour * (disc * 1.4 + glow);

  // Clouds: two drifting layers, thinning with cloudCover.
  float drift = iTime * 0.015;
  vec2 q = vec2(uv.x * 3.4 + drift, uv.y * 6.0 - drift * 0.6);
  float cloud = fbm(q + fbm(q * 0.7) * 0.6);
  cloud = smoothstep(0.52, 0.86, cloud) * smoothstep(0.02, 0.30, uv.y - horizon + 0.08);
  float cover = cloud * cloudCover;
  vec3 cloudColour = mix(vec3(0.42, 0.40, 0.50), vec3(1.0, 0.74, 0.58), smoothstep(0.0, 0.5, uv.y - horizon));
  col = mix(col, cloudColour, cover * 0.85);

  // Ground haze: a dark band that grounds the sky.
  col = mix(col, vec3(0.10, 0.08, 0.11), 1.0 - smoothstep(horizon - 0.10, horizon, uv.y));

  // Filmic-ish tone curve keeps the sun from clipping to flat white.
  col = col / (col + vec3(0.85));
  fragColor = vec4(pow(col, vec3(1.0 / 2.2)), 1.0);
}
