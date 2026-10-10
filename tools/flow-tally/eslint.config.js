// ESLint for the tally page. No package.json here: it reuses the frontend's packages,
// so run `npm ci` in frontend/ first, then from the repo root:
//   frontend/node_modules/.bin/eslint tools/flow-tally
import js from '../../frontend/node_modules/@eslint/js/src/index.js'
import globals from '../../frontend/node_modules/globals/index.js'

export default [
  js.configs.recommended,
  {
    files: ['**/*.js'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
]
