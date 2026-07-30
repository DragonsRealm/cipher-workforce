/**
 * Tests for LoginPage.
 *
 * The page previously had no tests at all, and three user-visible defects
 * lived here: server rejections were never displayed, the Register link
 * disappeared after one failed login, and the submit button never disabled
 * (so the form accepted unlimited concurrent requests). Each of those has a
 * case below.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import LoginPage from '../LoginPage';

const authState = {
  login: vi.fn(),
  register: vi.fn(),
  canRegister: true,
  error: null as string | null,
  submitError: null as string | null,
  isSubmitting: false,
  resetAuthErrors: vi.fn(),
};

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => authState,
}));

beforeEach(() => {
  vi.clearAllMocks();
  authState.login = vi.fn().mockResolvedValue(true);
  authState.register = vi.fn().mockResolvedValue(true);
  authState.canRegister = true;
  authState.error = null;
  authState.submitError = null;
  authState.isSubmitting = false;
  authState.resetAuthErrors = vi.fn();
});

const fillCredentials = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.type(screen.getByLabelText(/email/i), 'a@b.com');
  await user.type(screen.getByLabelText(/password/i), 'hunter2hunter2');
};

describe('LoginPage', () => {
  it('renders a heading and the sign-in form', () => {
    render(<LoginPage />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('OpenCompany');
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument();
  });

  it('submits credentials to login', async () => {
    const user = userEvent.setup();
    render(<LoginPage />);
    await fillCredentials(user);
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => {
      expect(authState.login).toHaveBeenCalledWith('a@b.com', 'hunter2hunter2');
    });
  });

  it('displays the server rejection in an alert', async () => {
    authState.submitError = 'Invalid email or password';
    render(<LoginPage />);

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Invalid email or password');
  });

  it('keeps the Register link visible when a login has failed', async () => {
    authState.submitError = 'Invalid email or password';
    authState.canRegister = true;
    render(<LoginPage />);

    expect(screen.getByRole('button', { name: /^register$/i })).toBeInTheDocument();
  });

  it('hides the Register link when registration is closed', () => {
    authState.canRegister = false;
    render(<LoginPage />);
    expect(screen.queryByRole('button', { name: /^register$/i })).not.toBeInTheDocument();
  });

  it('rejects a malformed email without calling login', async () => {
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText(/email/i), 'not-an-email');
    await user.type(screen.getByLabelText(/password/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByText(/valid email address/i)).toBeInTheDocument();
    });
    expect(authState.login).not.toHaveBeenCalled();
  });

  it('requires a password', async () => {
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText(/email/i), 'a@b.com');
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByText(/password is required/i)).toBeInTheDocument();
    });
    expect(authState.login).not.toHaveBeenCalled();
  });

  it('disables inputs and the button while submitting', () => {
    authState.isSubmitting = true;
    render(<LoginPage />);

    expect(screen.getByLabelText(/email/i)).toBeDisabled();
    expect(screen.getByLabelText(/password/i)).toBeDisabled();
    const button = screen.getByRole('button', { name: /please wait/i });
    expect(button).toBeDisabled();
  });

  it('does not fire a second request while one is in flight', async () => {
    const user = userEvent.setup();
    // A slow login keeps the mutation pending; the component is driven by
    // `isSubmitting`, so assert the disabled attribute blocks the second click.
    const { rerender } = render(<LoginPage />);
    await fillCredentials(user);

    authState.isSubmitting = true;
    rerender(<LoginPage />);

    const button = screen.getByRole('button', { name: /please wait/i });
    await user.click(button).catch(() => undefined);
    expect(authState.login).not.toHaveBeenCalled();
  });

  it('switches to register mode and requires a display name', async () => {
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.click(screen.getByRole('button', { name: /^register$/i }));
    expect(screen.getByRole('button', { name: /create account/i })).toBeInTheDocument();

    await user.type(screen.getByLabelText(/email/i), 'a@b.com');
    await user.type(screen.getByLabelText(/password/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /create account/i }));

    await waitFor(() => {
      expect(screen.getByText(/display name is required/i)).toBeInTheDocument();
    });
    expect(authState.register).not.toHaveBeenCalled();
  });

  it('enforces the 8-character minimum only when registering', async () => {
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.click(screen.getByRole('button', { name: /^register$/i }));
    await user.type(screen.getByLabelText(/display name/i), 'A');
    await user.type(screen.getByLabelText(/email/i), 'a@b.com');
    await user.type(screen.getByLabelText(/password/i), 'short');
    await user.click(screen.getByRole('button', { name: /create account/i }));

    await waitFor(() => {
      expect(screen.getByText(/at least 8 characters/i)).toBeInTheDocument();
    });
  });

  it('registers with a display name', async () => {
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.click(screen.getByRole('button', { name: /^register$/i }));
    await user.type(screen.getByLabelText(/display name/i), 'Alice');
    await user.type(screen.getByLabelText(/email/i), 'a@b.com');
    await user.type(screen.getByLabelText(/password/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /create account/i }));

    await waitFor(() => {
      expect(authState.register).toHaveBeenCalledWith('a@b.com', 'hunter2hunter2', 'Alice');
    });
  });

  it('clears stale errors when toggling mode', async () => {
    const user = userEvent.setup();
    render(<LoginPage />);
    await user.click(screen.getByRole('button', { name: /^register$/i }));
    expect(authState.resetAuthErrors).toHaveBeenCalled();
  });

  it('shows a connectivity error when there is no submit error', () => {
    authState.error = 'Failed to connect to server';
    render(<LoginPage />);
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to connect to server');
  });

  it('prefers the submit error over the connectivity error', () => {
    authState.error = 'Failed to connect to server';
    authState.submitError = 'Invalid email or password';
    render(<LoginPage />);

    const alerts = screen.getAllByRole('alert');
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent('Invalid email or password');
  });
});
