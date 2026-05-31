import { useEffect, useMemo, useState } from "react";

type NavigateOptions = string | URL;

function currentPathname(): string {
  if (typeof window === "undefined") {
    return "/";
  }
  return window.location.pathname || "/";
}

function currentSearch(): string {
  if (typeof window === "undefined") {
    return "";
  }
  return window.location.search || "";
}

function notifyNavigation(): void {
  window.dispatchEvent(new Event("vocalize:navigation"));
}

function navigate(to: NavigateOptions, mode: "push" | "replace"): void {
  const href = String(to);
  if (mode === "push") {
    window.history.pushState(null, "", href);
  } else {
    window.history.replaceState(null, "", href);
  }
  notifyNavigation();
}

export function useRouter() {
  return useMemo(
    () => ({
      push: (to: NavigateOptions) => navigate(to, "push"),
      replace: (to: NavigateOptions) => navigate(to, "replace"),
    }),
    [],
  );
}

export function usePathname(): string {
  const [pathname, setPathname] = useState(currentPathname);
  useEffect(() => {
    const update = () => setPathname(currentPathname());
    window.addEventListener("popstate", update);
    window.addEventListener("vocalize:navigation", update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener("vocalize:navigation", update);
    };
  }, []);
  return pathname;
}

export function useSearchParams(): URLSearchParams {
  const [search, setSearch] = useState(currentSearch);
  useEffect(() => {
    const update = () => setSearch(currentSearch());
    window.addEventListener("popstate", update);
    window.addEventListener("vocalize:navigation", update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener("vocalize:navigation", update);
    };
  }, []);
  return useMemo(() => new URLSearchParams(search), [search]);
}
