import { describe, expect, it } from 'vitest'

import {
  normalizeRencoOpenString,
  pathFromRencoDeepLink,
  pathFromOpenDeepLink,
  resolveRencoOpenPath
} from './renco-open-target'

describe('normalizeRencoOpenString', () => {
  it('accepts hash-router paths and strips a leading hash', () => {
    expect(normalizeRencoOpenString('/index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeRencoOpenString('#/index-network/intent/1')).toBe('/index-network/intent/1')
  })

  it('maps plugin-scoped renco:// deep links to the same path', () => {
    expect(normalizeRencoOpenString('renco://index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeRencoOpenString('renco://index-network/intent/1?focus=true')).toBe(
      '/index-network/intent/1?focus=true'
    )
  })

  it('maps renco://open/… deep links by stripping the open host', () => {
    expect(normalizeRencoOpenString('renco://open/index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeRencoOpenString('renco://open/settings/plugins')).toBe('/settings/plugins')
  })

  it('rejects reserved renco kinds and unsafe paths', () => {
    expect(normalizeRencoOpenString('renco://blueprint/morning-brief')).toBeNull()
    expect(normalizeRencoOpenString('renco://plugin/install')).toBeNull()
    expect(normalizeRencoOpenString('https://example.com/x')).toBeNull()
    expect(normalizeRencoOpenString('/../etc/passwd')).toBeNull()
    expect(normalizeRencoOpenString('index-network')).toBeNull()
  })
})

describe('resolveRencoOpenPath', () => {
  it('merges structured path + params', () => {
    expect(resolveRencoOpenPath({ path: '/index-network/intent/1', params: { focus: 'true' } })).toBe(
      '/index-network/intent/1?focus=true'
    )
  })

  it('resolves href the same as a bare string', () => {
    expect(resolveRencoOpenPath({ href: 'renco://index-network/intent/1' })).toBe('/index-network/intent/1')
  })
})

describe('pathFromRencoDeepLink', () => {
  it('builds the navigate path from a plugin-scoped deep-link payload', () => {
    expect(pathFromRencoDeepLink('index-network', 'intent/1')).toBe('/index-network/intent/1')
  })

  it('builds the navigate path from renco://open/… payloads', () => {
    expect(pathFromOpenDeepLink('index-network/intent/1')).toBe('/index-network/intent/1')
    expect(pathFromRencoDeepLink('open', 'agent/42')).toBe('/agent/42')
  })

  it('ignores reserved kinds', () => {
    expect(pathFromRencoDeepLink('blueprint', 'morning-brief')).toBeNull()
    expect(pathFromRencoDeepLink('plugin', 'install')).toBeNull()
  })
})
