"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { COOKIE_NAME, SESSION_MAX_AGE_SECONDS, createSessionToken } from "@/lib/auth";

export async function login(formData: FormData) {
  const password = formData.get("password");
  const from = (formData.get("from") as string) || "/";

  const enteredPassword = typeof password === "string" ? password.trim() : "";
  const expectedPassword = (process.env.ADMIN_PASSWORD || "").trim().replace(/^["']|["']$/g, "");

  if (!expectedPassword || enteredPassword !== expectedPassword) {
    redirect(`/login?error=1&from=${encodeURIComponent(from)}`);
  }

  const token = await createSessionToken();
  cookies().set(COOKIE_NAME, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    maxAge: SESSION_MAX_AGE_SECONDS,
    path: "/",
  });

  redirect(from.startsWith("/") ? from : "/");
}
