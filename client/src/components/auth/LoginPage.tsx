/**
 * Login/Register Page.
 *
 * Built on react-hook-form + zod through the shadcn `Form` primitives, the
 * same composition `EmailPanel` uses. That is not cosmetic: `FormControl`
 * emits `aria-invalid` and wires `aria-describedby` to the matching
 * `FormMessage`, which the previous hand-rolled markup did not do.
 *
 * Two failure signals are displayed, and they are not the same thing:
 * `submitError` is the server's rejection ("Invalid email or password"),
 * `error` is a connectivity failure from the bootstrap query. Before, only
 * the latter existed on the context, so a wrong password produced no
 * feedback whatsoever.
 */

import React, { useMemo, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';

import { useAuth } from '../../contexts/AuthContext';
import { Card, CardContent, CardHeader, CardTitle, CardDescription, CardFooter } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { createAuthFormSchema, type AuthFormValues } from './schemas/login';

const LoginPage: React.FC = () => {
  const {
    login,
    register,
    canRegister,
    error,
    submitError,
    isSubmitting,
    resetAuthErrors,
  } = useAuth();

  const [isRegistering, setIsRegistering] = useState(false);

  const schema = useMemo(() => createAuthFormSchema(isRegistering), [isRegistering]);

  const form = useForm<AuthFormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: '', password: '', displayName: '' },
    mode: 'onSubmit',
  });

  const onSubmit = async (values: AuthFormValues) => {
    resetAuthErrors();
    if (isRegistering) {
      await register(values.email, values.password, values.displayName ?? '');
    } else {
      await login(values.email, values.password);
    }
    // Failure text lives on `submitError`; nothing to do here. The mutation
    // never rejects out of these wrappers.
  };

  const toggleMode = () => {
    setIsRegistering((prev) => !prev);
    resetAuthErrors();
    form.clearErrors();
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-5">
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          {/* A real <h1>: the page previously had no heading landmark at all. */}
          <CardTitle className="text-3xl font-bold text-node-agent">
            <h1>OpenCompany</h1>
          </CardTitle>
          <CardDescription>
            {isRegistering ? 'Create your account' : 'Sign in to continue'}
          </CardDescription>
        </CardHeader>

        <CardContent className="space-y-4">
          {/* Server rejection: wrong password, duplicate email, rate limit. */}
          {submitError && (
            <Alert variant="destructive" aria-live="assertive">
              <AlertDescription>{submitError}</AlertDescription>
            </Alert>
          )}

          {/* Connectivity failure, distinct from a rejected credential. */}
          {error && !submitError && (
            <Alert variant="destructive" aria-live="polite">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <Form {...form}>
            {/* noValidate: zod owns validation. Otherwise the browser's own
                constraint check on type="email" silently blocks submit and
                shows a native bubble, which neither matches FormMessage
                styling nor respects the schema's rules. */}
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4" noValidate>
              {isRegistering && (
                <FormField
                  control={form.control}
                  name="displayName"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Display Name</FormLabel>
                      <FormControl>
                        <Input
                          placeholder="Your name"
                          autoComplete="name"
                          disabled={isSubmitting}
                          {...field}
                          value={field.value ?? ''}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              )}

              <FormField
                control={form.control}
                name="email"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Email</FormLabel>
                    <FormControl>
                      <Input
                        type="email"
                        placeholder="you@example.com"
                        autoComplete="email"
                        disabled={isSubmitting}
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="password"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Password</FormLabel>
                    <FormControl>
                      <Input
                        type="password"
                        placeholder={isRegistering ? 'At least 8 characters' : 'Your password'}
                        autoComplete={isRegistering ? 'new-password' : 'current-password'}
                        disabled={isSubmitting}
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <Button type="submit" className="w-full" disabled={isSubmitting}>
                {isSubmitting
                  ? 'Please wait...'
                  : isRegistering
                    ? 'Create Account'
                    : 'Sign In'}
              </Button>
            </form>
          </Form>
        </CardContent>

        {canRegister && (
          <CardFooter className="justify-center gap-2 border-t pt-4 text-sm">
            <span className="text-muted-foreground">
              {isRegistering ? 'Already have an account?' : "Don't have an account?"}
            </span>
            <Button
              type="button"
              variant="link"
              onClick={toggleMode}
              disabled={isSubmitting}
              className="h-auto p-0"
            >
              {isRegistering ? 'Sign In' : 'Register'}
            </Button>
          </CardFooter>
        )}
      </Card>
    </div>
  );
};

export default LoginPage;
