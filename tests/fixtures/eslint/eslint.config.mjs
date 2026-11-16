import { readFileSync } from 'node:fs';
import eslint from '@eslint/js';
import tseslint from 'typescript-eslint';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import prettier from 'eslint-config-prettier';

const exceptions = JSON.parse(readFileSync(new URL('./eslint-exceptions.json', import.meta.url), 'utf8'));
const react19Message = 'React 19 전용 API 는 쓰지 않는다';
const react19Names = {
    react: new Set(['use', 'useActionState', 'useOptimistic']),
    'react-dom': new Set(['useFormStatus', 'preload', 'preinit', 'preconnect', 'prefetchDNS']),
};
const apiRules = {
    'app/no-react19-api': 'error',
    'no-restricted-syntax': ['error', {
        selector: 'Identifier[name="WebSocket"]:not(MemberExpression > Identifier.property):not(Property > Identifier.key):not(TSPropertySignature > Identifier.key)',
        message: 'WebSocket 은 쓰지 않는다',
    }, {
        selector: 'MemberExpression[computed=false][property.name="WebSocket"], MemberExpression[computed=true][property.value="WebSocket"]',
        message: 'WebSocket 은 쓰지 않는다',
    }],
    'no-restricted-properties': ['error', {
        object: 'Promise',
        property: 'withResolvers',
        message: 'Promise.withResolvers 는 쓰지 않는다',
    }],
    '@typescript-eslint/no-restricted-imports': ['error', {
        paths: [{
            name: 'axios',
            allowTypeImports: true,
            message: 'API 요청은 utils/auth.ts 를 거친다',
        }],
    }],
};

for (const [rule, files] of Object.entries(exceptions)) {
    if (!Object.hasOwn(apiRules, rule)) throw new Error(`알 수 없는 예외 규칙: ${rule}`);
    if (!Array.isArray(files) || files.some((file) => typeof file !== 'string' || !file.startsWith('src/') || /[*!?{}\[\]]/.test(file))) {
        throw new Error(`예외는 src/ 로 시작하는 파일 경로 배열이어야 한다: ${rule}`);
    }
}

export default tseslint.config(
    {
        ignores: ['dist/', 'node_modules/', 'webpack.*.js', 'babel.config.mjs', 'postcss.config.*'],
    },

    eslint.configs.recommended,

    ...tseslint.configs.recommended,

    {
        files: ['src/**/*.{ts,tsx}'],
        plugins: {
            react,
            'react-hooks': reactHooks,
        },
        languageOptions: {
            parserOptions: {
                ecmaFeatures: { jsx: true },
            },
        },
        settings: {
            react: { version: 'detect' },
        },
        rules: {
            'react/react-in-jsx-scope': 'off',
            'react/prop-types': 'off',
            'react/display-name': 'off',

            'react-hooks/rules-of-hooks': 'error',
            'react-hooks/exhaustive-deps': 'warn',

            '@typescript-eslint/no-explicit-any': 'warn',
            '@typescript-eslint/no-unused-vars': ['warn', {
                argsIgnorePattern: '^_',
                varsIgnorePattern: '^_',
                caughtErrorsIgnorePattern: '^_',
            }],
            '@typescript-eslint/no-require-imports': 'off',

            'no-console': ['warn', {
                allow: ['warn', 'error', 'debug'],
            }],

            'no-debugger': 'error',
            'no-duplicate-imports': 'warn',
            'prefer-const': 'warn',

            'no-var': 'warn',
            'no-empty': 'warn',
            'no-empty-pattern': 'warn',
            'no-fallthrough': 'warn',
            'no-case-declarations': 'warn',
            'no-prototype-builtins': 'warn',
            '@typescript-eslint/no-unused-expressions': 'warn',
            'no-useless-assignment': 'warn',
        },
    },

    {
        files: ['src/**/*.{js,jsx,ts,tsx}'],
        plugins: {
            app: {
                rules: {
                    'no-react19-api': {
                        meta: { schema: [], messages: { forbidden: react19Message } },
                        create(context) {
                            return {
                                'ImportDeclaration, ExportNamedDeclaration'(node) {
                                    if (!node.source || !Object.hasOwn(react19Names, node.source.value)) return;
                                    const forbidden = react19Names[node.source.value];
                                    for (const specifier of node.specifiers) {
                                        const name = specifier.type === 'ImportSpecifier' ? specifier.imported : specifier.type === 'ExportSpecifier' ? specifier.local : null;
                                        if (name && forbidden.has(name.name ?? name.value)) {
                                            context.report({ node: specifier, messageId: 'forbidden' });
                                        }
                                    }
                                },
                                'Program:exit'(program) {
                                    for (const node of program.body) {
                                        if (node.type !== 'ImportDeclaration') continue;
                                        const forbidden = react19Names[node.source.value];
                                        if (!Object.hasOwn(react19Names, node.source.value)) continue;
                                        for (const variable of context.sourceCode.getDeclaredVariables(node)) {
                                            if (!variable.defs.some(({ node: specifier }) => specifier.type === 'ImportNamespaceSpecifier' || specifier.type === 'ImportDefaultSpecifier')) continue;
                                            for (const { identifier } of variable.references) {
                                                const parent = identifier.parent;
                                                if (parent.type === 'MemberExpression' && parent.object === identifier && forbidden.has(parent.computed ? parent.property.value : parent.property.name)) {
                                                    context.report({ node: parent, messageId: 'forbidden' });
                                                }
                                                if (parent.type === 'VariableDeclarator' && parent.init === identifier && parent.id.type === 'ObjectPattern') {
                                                    for (const property of parent.id.properties) {
                                                        if (property.type === 'Property' && forbidden.has(property.key.name ?? property.key.value)) {
                                                            context.report({ node: property, messageId: 'forbidden' });
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                },
                            };
                        },
                    },
                },
            },
        },
        rules: apiRules,
    },
    {
        files: ['src/utils/auth.ts', 'src/utils/requestAuth.ts', 'src/**/__tests__/**', 'src/**/*.test.*'],
        rules: { '@typescript-eslint/no-restricted-imports': 'off' },
    },
    ...Object.entries(exceptions).filter(([, files]) => files.length > 0).map(([rule, files]) => ({
        files, rules: { [rule]: 'off' },
    })),
    prettier,
);