import '@mantine/core/styles.css';
import Head from 'next/head';
import { MantineProvider } from '@mantine/core';

import { theme } from '../theme';
import '../public/static/pulse.css';

export default function App({ Component, pageProps }: any) {
  return (
    <MantineProvider theme={theme}>
      <Head>
        <title>Flowcast</title>
        <meta
          name='viewport'
          content='minimum-scale=1, initial-scale=1, width=device-width, user-scalable=no'
        />
        <link rel='shortcut icon' href='/static/favicon.png' />
      </Head>
      <Component {...pageProps} />
    </MantineProvider>
  );
}
