import { unstable_setRequestLocale } from "next-intl/server";
import { CreateSessionClient } from "./CreateSessionClient";

interface Props {
  params: { locale: string };
}

export default function NewSessionPage({ params }: Props) {
  unstable_setRequestLocale(params.locale);
  return <CreateSessionClient />;
}
