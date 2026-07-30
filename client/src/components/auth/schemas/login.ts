import { z } from 'zod';

/**
 * Validation schema for the login / register form.
 *
 * Mirrors the shape of `credentials/panels/schemas/email.ts` so both forms
 * are validated the same way. Previously this was hand-rolled inside
 * `LoginPage`, which meant email format was never checked client-side --
 * the server uses `EmailStr`, so a malformed address came back as a 422
 * whose `detail` is a list of objects rather than a string.
 */
export function createAuthFormSchema(isRegistering: boolean) {
  return z.object({
    email: z
      .string()
      .min(1, 'Email is required')
      .pipe(z.email('Enter a valid email address')),
    password: isRegistering
      ? z.string().min(8, 'Password must be at least 8 characters')
      : z.string().min(1, 'Password is required'),
    // Required only when registering. The 100-character ceiling mirrors the
    // `max_length=100` column on `User.display_name`; SQLite truncates
    // silently rather than raising, so an over-long name would be accepted
    // and then quietly altered.
    displayName: isRegistering
      ? z.string().trim().min(1, 'Display name is required').max(100, 'Display name must be 100 characters or fewer')
      : z.string().optional(),
  });
}

export type AuthFormValues = z.infer<ReturnType<typeof createAuthFormSchema>>;
