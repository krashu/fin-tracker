/**
 * /imports/review/[batchId]. The live tag → stage → commit loop is the
 * `ReviewQueue` client island; global chrome comes from `AppShell` in
 * app/layout.tsx. Reached by redirect from the upload page (POST /imports →
 * batch_id), not a nav item — there's no batch-list endpoint in v1. The extra
 * bottom padding clears the queue's fixed action bar.
 */
import { Suspense } from "react";
import { notFound } from "next/navigation";

import { ReviewQueue } from "./review-queue";

export default async function ReviewPage({
  params,
}: {
  params: Promise<{ batchId: string }>;
}) {
  const { batchId } = await params;
  const id = Number(batchId);

  // A malformed id needs no API call to reject, so it gets a genuine 404 (HTTP
  // status + app/not-found.tsx). A well-formed-but-missing batch can't be
  // checked here — auth cookies are httpOnly on the backend origin, out of
  // reach of server components (see RouteGuard) — so ReviewQueue renders that
  // case from the API's 404 instead.
  if (!Number.isInteger(id) || id < 1) notFound();

  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 40px 96px" }}>
      {/* Suspense boundary: ReviewQueue reads useSearchParams (?present carries
          the re-upload's already-imported count), which requires one. */}
      <Suspense>
        <ReviewQueue batchId={id} />
      </Suspense>
    </div>
  );
}
