/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT_DIR = path.resolve(__dirname, '..');
const APP_JS_PATH = path.join(ROOT_DIR, 'app', 'static', 'app.js');
const INDEX_HTML_PATH = path.join(ROOT_DIR, 'app', 'static', 'index.html');
const STYLES_CSS_PATH = path.join(ROOT_DIR, 'app', 'static', 'styles.css');

/**
 * Creates a lightweight DOM element mock for unit testing.
 * @param {string=} id Optional element ID.
 * @return {!Object} Mock DOM element.
 */
function createMockElement(id = '') {
  const attributes = new Map();
  const children = [];
  const classes = new Set();
  const styleProps = new Map();

  return {
    id,
    value: '',
    textContent: '',
    disabled: false,
    checked: false,
    dataset: {},
    children,
    style: {
      display: '',
      width: '',
      setProperty(prop, val) {
        styleProps.set(prop, String(val));
      },
      getPropertyValue(prop) {
        return styleProps.get(prop) || '';
      },
    },
    classList: {
      add(...tokens) {
        for (const token of tokens) {
          classes.add(token);
        }
      },
      remove(...tokens) {
        for (const token of tokens) {
          classes.delete(token);
        }
      },
      toggle(token, force) {
        if (force === undefined) {
          if (classes.has(token)) {
            classes.delete(token);
            return false;
          }
          classes.add(token);
          return true;
        }
        if (force) {
          classes.add(token);
        } else {
          classes.delete(token);
        }
        return Boolean(force);
      },
      contains(token) {
        return classes.has(token);
      },
    },
    setAttribute(name, val) {
      attributes.set(name, String(val));
    },
    getAttribute(name) {
      return attributes.has(name) ? attributes.get(name) : null;
    },
    removeAttribute(name) {
      attributes.delete(name);
    },
    appendChild(child) {
      children.push(child);
      return child;
    },
    replaceChildren(...newChildren) {
      children.length = 0;
      children.push(...newChildren);
    },
    addEventListener() {},
    querySelector() {
      return createMockElement();
    },
    querySelectorAll() {
      return [];
    },
  };
}

/**
 * Loads app.js inside an isolated VM sandbox and returns its test hooks.
 * @return {!Object} Loaded frontend hooks and mock DOM state.
 */
function loadFrontendHooks() {
  const elementsById = new Map();
  const storage = new Map();

  /**
   * Returns or creates a mock DOM element by ID.
   * @param {string} id Element ID.
   * @return {!Object} Mock DOM element.
   */
  function getElementById(id) {
    if (!elementsById.has(id)) {
      elementsById.set(id, createMockElement(id));
    }
    return elementsById.get(id);
  }

  const documentMock = {
    documentElement: {lang: 'ja'},
    body: createMockElement('body'),
    getElementById,
    createElement(tag) {
      return createMockElement(tag);
    },
    querySelector() {
      return createMockElement();
    },
    querySelectorAll() {
      return [];
    },
    addEventListener() {},
  };

  const windowMock = {
    location: {
      pathname: '/',
      origin: 'http://127.0.0.1:8080',
      href: 'http://127.0.0.1:8080/',
    },
    history: {
      pushState() {},
      replaceState() {},
    },
    localStorage: {
      getItem(key) {
        return storage.has(key) ? storage.get(key) : null;
      },
      setItem(key, val) {
        storage.set(key, String(val));
      },
    },
    addEventListener() {},
    setTimeout() {
      return 1;
    },
    clearTimeout() {},
  };

  const sandbox = {
    window: windowMock,
    document: documentMock,
    localStorage: windowMock.localStorage,
    history: windowMock.history,
    location: windowMock.location,
    fetch: async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        authenticated: false,
        csrf_token: 'mock-csrf',
        today_yyyymmdd: '20261006',
      }),
    }),
    setTimeout: windowMock.setTimeout,
    clearTimeout: windowMock.clearTimeout,
    encodeURIComponent,
    decodeURIComponent,
    Math,
    Date,
    Number,
    String,
    Boolean,
    Array,
    Object,
    Set,
    Map,
    RegExp,
    JSON,
    Error,
    parseInt,
    parseFloat,
    isNaN,
    isFinite,
  };
  sandbox.globalThis = sandbox;

  const code = fs.readFileSync(APP_JS_PATH, 'utf8');
  vm.runInNewContext(code, sandbox, {filename: 'app.js'});
  return {
    hooks: sandbox.__IMAGE_UPSCALER_TEST_HOOKS__,
    elementsById,
    documentMock,
    storage,
  };
}

test('static files carry Apache 2.0 headers and strict XSS hygiene', () => {
  for (const filePath of [APP_JS_PATH, INDEX_HTML_PATH, STYLES_CSS_PATH]) {
    const content = fs.readFileSync(filePath, 'utf8');
    assert.match(content, /Copyright 2026 Google LLC/);
    assert.match(content, /Licensed under the Apache License, Version 2\.0/);
  }

  const appJs = fs.readFileSync(APP_JS_PATH, 'utf8');
  assert.equal(appJs.includes('.innerHTML'), false);
  assert.equal(appJs.includes('.outerHTML'), false);
  assert.equal(appJs.includes('document.write('), false);
});

test('APAC 9-language localization dictionary and persistence work', () => {
  const {hooks, documentMock, storage} = loadFrontendHooks();
  const expectedLangs = [
    'ja',
    'en',
    'zh-CN',
    'zh-TW',
    'ko',
    'vi',
    'th',
    'id',
    'hi',
  ];
  for (const lang of expectedLangs) {
    assert.ok(hooks.I18N[lang], `Missing language dictionary for ${lang}`);
    assert.ok(hooks.I18N[lang].nav_requests);
    assert.ok(hooks.I18N[lang].nav_billing);
  }

  hooks.applyLanguage('en');
  assert.equal(documentMock.documentElement.lang, 'en');
  assert.equal(storage.get('gmc_ui_lang'), 'en');
  assert.equal(hooks.t('filter_all'), 'All');

  hooks.applyLanguage('ja');
  assert.equal(documentMock.documentElement.lang, 'ja');
  assert.equal(storage.get('gmc_ui_lang'), 'ja');
});

test('cost, USD, and duration estimators calculate expected values', () => {
  const {hooks} = loadFrontendHooks();

  assert.equal(hooks.formatUsd(0), '$0.0000');
  assert.equal(hooks.formatUsd(0.0168), '$0.0168');
  assert.equal(hooks.formatUsd(12.5), '$12.5000');

  const lanczosCost = hooks.estimateJobCost('local-lanczos-canvas', 1500, 1000);
  assert.ok(lanczosCost > 0 && lanczosCost < 0.05);

  const liteCost = hooks.estimateJobCost(
    'gemini-3.1-flash-lite-image',
    1500,
    100
  );
  assert.ok(Math.abs(liteCost - 1.7) < 0.05);

  assert.match(hooks.formatDurationEstimate(10, 20), /sec$/);
  assert.match(hooks.formatDurationEstimate(600, 2), /m/);
});

test('comparator split and preview proxy URL builder work safely', () => {
  const {hooks, elementsById} = loadFrontendHooks();

  const proxyUrl = hooks.buildPreviewProxyUrl(
    'https://example.com/item.jpg',
    'gfl1500'
  );
  assert.equal(
    proxyUrl,
    '/api/preview-source?url=https%3A%2F%2Fexample.com%2Fitem.jpg&v=gfl1500'
  );
  assert.equal(hooks.buildPreviewProxyUrl(''), '');

  hooks.setComparatorSplit(35);
  const stageEl = elementsById.get('sku-comp-stage');
  assert.equal(stageEl.style.getPropertyValue('--split-pct'), '35%');
});

test('route parsing, CSV row counting, and ETA formatting work', () => {
  const {hooks} = loadFrontendHooks();

  assert.equal(hooks.parseRouteRequestId('/request/job-abc123'), 'job-abc123');
  assert.equal(hooks.parseRouteRequestId('/billing'), null);

  const sampleCsv = hooks.getSampleCsvString();
  assert.ok(sampleCsv.includes('additional_image_link'));
  assert.equal(hooks.countCsvRows(sampleCsv), 3);

  const multiCsv =
    'id,image_link,additional_image_link\n' +
    'SKU-1,https://example.com/1.jpg,' +
    '"https://example.com/2.jpg,https://example.com/3.jpg"\n';
  assert.equal(hooks.countCsvRows(multiCsv), 3);

  assert.equal(hooks.formatSecondsCompact(45), '45s');
  assert.equal(hooks.formatSecondsCompact(125), '2m 5s');
});
