const map = {
  '@hermes/plugin-sdk': './stubs/sdk.mjs',
  react: './stubs/react.mjs',
  'react/jsx-runtime': './stubs/jsx.mjs',
}
export async function resolve(spec, ctx, next) {
  return map[spec] ? next(new URL(map[spec], import.meta.url).href, ctx) : next(spec, ctx)
}
