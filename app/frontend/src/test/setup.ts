import '@testing-library/jest-dom/vitest'
import { vi } from 'vitest'

// UI fixtures exercise their API payloads; session transport has its own tests.
vi.mock('../api/localSession', () => ({
  localSession: vi.fn(async () => 'test-session-credential-that-is-never-used-in-production'),
  clearLocalSession: vi.fn(),
}))

