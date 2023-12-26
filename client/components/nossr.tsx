import dynamic from 'next/dynamic';
import { FC, PropsWithChildren} from 'react';

const NoSSR: FC<PropsWithChildren> = ({ children }) => {
  return <>{children}</>;
};

export default dynamic(() => Promise.resolve(NoSSR), {
  ssr: false
});
