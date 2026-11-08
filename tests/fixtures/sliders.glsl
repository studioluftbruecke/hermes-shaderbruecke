// Slider fixture: a striped plasma whose look is entirely the three sliders.
// @slider frequency 1.0..12.0 = 5.0 step 0.1
// @slider warp 0.0..2.0 = 0.8 step 0.05
// @slider palette 0.0..1.0 = 0.35 step 0.01

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
  vec2 uv = (fragCoord - 0.5 * iResolution.xy) / iResolution.y;
  float r = length(uv);
  float a = atan(uv.y, uv.x);

  float bands = sin(frequency * (r * 6.283 + a * warp) + iTime * 0.8);
  float rings = cos(frequency * r * 6.283 - iTime * 0.5) * 0.5 + 0.5;

  vec3 warm = vec3(1.0, 0.45, 0.15);
  vec3 cool = vec3(0.15, 0.55, 1.0);
  vec3 col = mix(cool, warm, palette);
  col *= 0.55 + 0.45 * bands;
  col *= 0.7 + 0.3 * rings;
  col *= 1.0 - smoothstep(0.45, 0.65, r) * 0.6;

  fragColor = vec4(pow(max(col, 0.0), vec3(1.0 / 2.2)), 1.0);
}
