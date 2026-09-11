// ESLint flat configuration for the staged storefront (astronomy-concierge overlay).
//
// Upstream 3.0.0 ships an .eslintrc that the pinned ESLint 9 cannot load and a `next lint`
// script that Next.js 16 removed, so `npm run lint` never ran. This file reproduces that
// configuration with Next's own flat presets and the upstream rule overrides. Overlay code is
// linted with the full rule set; the relaxations at the end cover unchanged upstream files
// that `next lint` never covered or that predate the react-hooks 7 rules.
import { defineConfig, globalIgnores } from 'eslint/config';
import nextCoreWebVitals from 'eslint-config-next/core-web-vitals';
import nextTypescript from 'eslint-config-next/typescript';

export default defineConfig([
  globalIgnores(['.next/**', 'protos/**', 'next-env.d.ts']),
  ...nextCoreWebVitals,
  ...nextTypescript,
  {
    rules: {
      '@typescript-eslint/no-non-null-assertion': 'off',
      'react-hooks/exhaustive-deps': 'warn',
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': 'error',
      'max-len': [
        'error',
        {
          code: 150,
          ignoreComments: true,
          ignoreTrailingComments: true,
          ignoreUrls: true,
          ignoreStrings: true,
          ignoreTemplateLiterals: true,
        },
      ],
    },
  },
  {
    // CommonJS files that Node loads directly; they cannot use import.
    files: ['next.config.js', 'utils/telemetry/Instrumentation.js'],
    rules: { '@typescript-eslint/no-require-imports': 'off' },
  },
  {
    // Released 3.0.0 files with pre-existing findings; they stay as upstream wrote them.
    files: [
      'components/Ad/Ad.tsx',
      'components/Footer/Footer.tsx',
      'components/ProductCard/ProductCard.tsx',
      'cypress.config.ts',
      'pages/product/\\[productId\\]/index.tsx', // brackets escaped: minimatch character class
      'providers/Ad.provider.tsx',
      'providers/Cart.provider.tsx',
      'providers/Currency.provider.tsx',
      'utils/Request.ts',
    ],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-unused-vars': 'off',
      'max-len': 'off',
      'react-hooks/purity': 'off',
      'react-hooks/set-state-in-effect': 'off',
    },
  },
]);
