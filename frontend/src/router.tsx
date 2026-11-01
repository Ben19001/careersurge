import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { Shell } from "@/layout";
import { ApproveScreen } from "@/screens/approve";
import { NeedsYouScreen } from "@/screens/needs-you";
import { OnboardingScreen } from "@/screens/onboarding";
import { ProfileScreen } from "@/screens/profile";
import { ProgressScreen } from "@/screens/progress";
import { SettingsScreen } from "@/screens/settings";

const rootRoute = createRootRoute({ component: Shell });
const routeTree = rootRoute.addChildren([
  createRoute({ getParentRoute: () => rootRoute, path: "/", component: ApproveScreen }),
  createRoute({ getParentRoute: () => rootRoute, path: "/progress", component: ProgressScreen }),
  createRoute({ getParentRoute: () => rootRoute, path: "/needs-you", component: NeedsYouScreen }),
  createRoute({ getParentRoute: () => rootRoute, path: "/profile", component: ProfileScreen }),
  createRoute({ getParentRoute: () => rootRoute, path: "/settings", component: SettingsScreen }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/onboarding",
    component: OnboardingScreen,
  }),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
