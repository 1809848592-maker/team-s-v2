export interface Account {
  id: string
  username: string
  is_admin: boolean
}

export class AuthError extends Error {
  constructor(public readonly status: number) {
    super('Account request failed')
  }
}

async function request(path: string, credentials?: { username: string; password: string }) {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 10000)

  try {
    const isPost = path !== 'me'
    const response = await fetch(`/api/auth/${path}`, {
      method: isPost ? 'POST' : 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: isPost
        ? {
          'X-CSRF-Protection': '1',
          ...(credentials ? { 'Content-Type': 'application/json' } : {}),
        }
        : undefined,
      body: credentials ? JSON.stringify(credentials) : undefined,
      signal: controller.signal,
    })

    if (!response.ok) throw new AuthError(response.status)
    if (path === 'logout') return

    const account: unknown = await response.json()
    if (typeof account !== 'object' || account === null
      || !('id' in account) || typeof account.id !== 'string'
      || !('username' in account) || typeof account.username !== 'string'
      || !('is_admin' in account) || typeof account.is_admin !== 'boolean') {
      throw new Error('Unexpected account response')
    }
    return account as Account
  } finally {
    window.clearTimeout(timeout)
  }
}

export async function getAccount(): Promise<Account> {
  return (await request('me'))!
}

export async function signIn(mode: 'login' | 'register', username: string, password: string): Promise<Account> {
  return (await request(mode, { username, password }))!
}

export async function signOut(): Promise<void> {
  await request('logout')
}
