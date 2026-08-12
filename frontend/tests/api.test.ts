import { describe, expect, it } from 'vitest'
import { formatTime, getCsrfToken, setCsrfToken, statusType } from '../src/api'

describe('frontend utilities', () => {
  it('stores the csrf token in session storage', () => { setCsrfToken('token-1'); expect(getCsrfToken()).toBe('token-1') })
  it('maps task states to visual types', () => { expect(statusType('SUCCESS')).toBe('success'); expect(statusType('FAILED')).toBe('danger'); expect(statusType('WAITING')).toBe('warning') })
  it('renders absent time consistently', () => { expect(formatTime(null)).toBe('—') })
})

