import '@mantine/core/styles.css';
import Head from 'next/head';
import { MantineProvider } from '@mantine/core';
import TagManager from 'react-gtm-module';
import { useEffect } from 'react';

import { theme } from '../theme';
import '../public/static/pulse.css';

const GTAG_ID = 'G-5543VQL2Z1';

export default function App({ Component, pageProps }: any) {
  useEffect(() => {
    TagManager.initialize({
      gtmId: GTAG_ID
    });
  });

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
